#!/usr/bin/env python3
"""Trainer 1 — supervised re-train of the relevance ENCODER.

Reads the audit + feedback logs, joins them into ``(task, episode_text) -> label``
examples, fine-tunes a small cross-encoder (3-way: KEEP / SUMMARIZE / DROP), and
exports it to ONNX so ``relevance_encoder.EncoderSuggester(model_path=…)`` can load
it locally in both the website and the extension.

This is the "improve the encoder from human interaction" loop. It's plain
supervised learning on the user's confirmed labels (active-learning weighted
toward the cases the user *overrode*), not RLHF — the right tool for a classifier.

Usage (uv, per project convention):

    # See what data you have — no ML libraries needed:
    uv run python backend/training/train_encoder.py --dry-run

    # Cold-start by also distilling the judge's own labels (silver):
    uv run python backend/training/train_encoder.py --dry-run --include-model-labels

    # Actually train + export ONNX (installs torch/transformers/optimum):
    uv run python backend/training/train_encoder.py \
        --base-model cross-encoder/ms-marco-MiniLM-L6-v2 \
        --epochs 3 --out ~/.acm/models/relevance-encoder

Safety metric that matters most: **keep-recall** — of episodes the user actually
kept, how few we'd have suggested dropping. The eval prints it; keep it ≥ 0.98.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _dataset import (  # noqa: E402
    VALID_LABELS,
    build_examples,
    group_holdout,
    label_counts,
    override_rate,
)

LABEL2ID = {lbl: i for i, lbl in enumerate(VALID_LABELS)}  # KEEP=0, SUMMARIZE=1, DROP=2


def _print_stats(examples) -> None:
    gold = [e for e in examples if e.gold]
    silver = [e for e in examples if not e.gold]
    print(f"  total examples : {len(examples)}  (gold={len(gold)}, silver={len(silver)})")
    print(f"  label balance  : {label_counts(examples)}")
    print(f"  user-override  : {override_rate(examples) * 100:.1f}%  (gold rows where user ≠ model)")
    convs = {e.conv for e in examples}
    print(f"  conversations  : {len(convs)}")


def _sample_weights(examples):
    """Up-weight the rows the user corrected (active learning) and gold over
    silver. Returns a list aligned with ``examples``."""
    w = []
    for e in examples:
        x = 1.0
        if not e.gold:
            x *= 0.5  # silver (judge label) counts for less than a human one
        if e.gold and e.model_label in VALID_LABELS and e.label != e.model_label:
            x *= 3.0  # the model got this wrong — learn it hard
        w.append(x)
    return w


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--audits", type=Path, default=None, help="audits jsonl (default ~/.acm/relevance_audits.jsonl)")
    ap.add_argument("--feedback", type=Path, default=None, help="feedback jsonl (default ~/.acm/relevance_feedback.jsonl)")
    ap.add_argument("--include-model-labels", action="store_true", help="add judge labels as silver examples (cold-start)")
    ap.add_argument("--base-model", default="cross-encoder/ms-marco-MiniLM-L6-v2")
    ap.add_argument("--out", type=Path, default=Path.home() / ".acm" / "models" / "relevance-encoder")
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--max-length", type=int, default=384)
    ap.add_argument("--eval-frac", type=float, default=0.2)
    ap.add_argument("--min-examples", type=int, default=50, help="refuse to train below this many gold examples")
    ap.add_argument("--dry-run", action="store_true", help="build + report the dataset only; no training")
    args = ap.parse_args()

    examples = build_examples(
        audit_path=args.audits,
        feedback_path=args.feedback,
        include_model_labels=args.include_model_labels,
    )
    print("Dataset")
    _print_stats(examples)
    train, ev = group_holdout(examples, frac=args.eval_frac)
    print(f"  split          : train={len(train)}  eval={len(ev)} (grouped by conversation)")

    if args.dry_run:
        print("\n[dry-run] no training. Re-run without --dry-run to fine-tune + export ONNX.")
        return 0

    gold_n = sum(1 for e in examples if e.gold)
    if gold_n < args.min_examples:
        print(f"\nRefusing to train: only {gold_n} gold examples (< --min-examples {args.min_examples}).")
        print("Collect more user decisions first, or pass --include-model-labels for a silver cold-start.")
        return 1

    # ── heavy path: import ML libs lazily so --dry-run never needs them ──
    try:
        import numpy as np
        import torch
        from torch.utils.data import Dataset
        from transformers import (
            AutoModelForSequenceClassification,
            AutoTokenizer,
            Trainer,
            TrainingArguments,
        )
    except ImportError as e:
        print(f"\nTraining needs ML libraries that aren't installed: {e}")
        print("Install them, e.g.:  uv pip install 'torch' 'transformers' 'optimum[onnxruntime]'")
        return 2

    tok = AutoTokenizer.from_pretrained(args.base_model)

    class DS(Dataset):
        def __init__(self, rows, weights):
            self.rows = rows
            self.weights = weights

        def __len__(self):
            return len(self.rows)

        def __getitem__(self, i):
            e = self.rows[i]
            enc = tok(
                e.task or "", e.episode_text or "",
                truncation=True, max_length=args.max_length, padding="max_length",
            )
            enc = {k: torch.tensor(v) for k, v in enc.items()}
            enc["labels"] = torch.tensor(LABEL2ID.get(e.label, 0))
            enc["weight"] = torch.tensor(self.weights[i], dtype=torch.float)
            return enc

    class WeightedTrainer(Trainer):
        def compute_loss(self, model, inputs, return_outputs=False, **kw):
            weights = inputs.pop("weight")
            labels = inputs.pop("labels")
            out = model(**inputs)
            per = torch.nn.functional.cross_entropy(out.logits, labels, reduction="none")
            loss = (per * weights).mean()
            return (loss, out) if return_outputs else loss

    model = AutoModelForSequenceClassification.from_pretrained(
        args.base_model, num_labels=len(VALID_LABELS),
        id2label={i: l for l, i in LABEL2ID.items()}, label2id=LABEL2ID,
    )
    train_ds = DS(train, _sample_weights(train))

    args.out.mkdir(parents=True, exist_ok=True)
    trainer = WeightedTrainer(
        model=model,
        args=TrainingArguments(
            output_dir=str(args.out / "_hf"),
            num_train_epochs=args.epochs,
            per_device_train_batch_size=args.batch_size,
            logging_steps=10,
            save_strategy="no",
            report_to=[],
        ),
        train_dataset=train_ds,
    )
    trainer.train()

    # ── eval (the numbers that decide if we promote this model) ──────────
    if ev:
        model.eval()
        ids, gold = [], []
        with torch.no_grad():
            for e in ev:
                enc = tok(e.task or "", e.episode_text or "", truncation=True,
                          max_length=args.max_length, return_tensors="pt")
                pred = int(model(**enc).logits.argmax(-1)[0])
                ids.append(pred)
                gold.append(LABEL2ID.get(e.label, 0))
        _report_eval(np, gold, ids)

    # ── export ONNX (what EncoderSuggester loads) ────────────────────────
    onnx_dir = args.out
    try:
        from optimum.onnxruntime import ORTModelForSequenceClassification

        ort = ORTModelForSequenceClassification.from_pretrained(
            str(args.out / "_hf_model"), export=True
        ) if (args.out / "_hf_model").exists() else None
        model.save_pretrained(args.out / "_hf_model")
        tok.save_pretrained(args.out / "_hf_model")
        ort = ORTModelForSequenceClassification.from_pretrained(str(args.out / "_hf_model"), export=True)
        ort.save_pretrained(onnx_dir)
        tok.save_pretrained(onnx_dir)
        print(f"\nExported ONNX to {onnx_dir}")
        print(f"Point the encoder at it:  export ACM_ENCODER_PATH={onnx_dir}")
    except ImportError:
        model.save_pretrained(onnx_dir / "_hf_model")
        tok.save_pretrained(onnx_dir / "_hf_model")
        print(f"\nSaved HF model to {onnx_dir / '_hf_model'} (install optimum[onnxruntime] to export ONNX).")
    return 0


def _report_eval(np, gold, pred) -> None:
    gold = np.array(gold)
    pred = np.array(pred)
    print("\nEval (grouped hold-out)")
    n = len(gold)
    acc = float((gold == pred).mean()) if n else 0.0
    print(f"  accuracy       : {acc:.3f}  (n={n})")
    keep_id = LABEL2ID["KEEP"]
    drop_id = LABEL2ID["DROP"]
    # keep-recall: of true KEEP, how many predicted KEEP (safety).
    true_keep = gold == keep_id
    keep_recall = float((pred[true_keep] == keep_id).mean()) if true_keep.any() else 1.0
    # drop-precision: of predicted DROP, how many were truly DROP.
    pred_drop = pred == drop_id
    drop_prec = float((gold[pred_drop] == drop_id).mean()) if pred_drop.any() else 1.0
    print(f"  keep-recall    : {keep_recall:.3f}   <-- keep ≥ 0.98 (a wrong drop is costly)")
    print(f"  drop-precision : {drop_prec:.3f}")
    print("  confusion (rows=true KEEP/SUMM/DROP, cols=pred):")
    for ti, tl in enumerate(VALID_LABELS):
        row = [int(((gold == ti) & (pred == pj)).sum()) for pj in range(len(VALID_LABELS))]
        print(f"    {tl:9s} {row}")


if __name__ == "__main__":
    raise SystemExit(main())
