#!/usr/bin/env python3
"""Trainer 2 — improve the JUDGE (LLM-as-judge) from human interaction.

Two outputs, both built from the same joined audit+feedback data. Neither needs a
GPU to *produce*; Tier 1 needs nothing further, Tier 2 hands off to TRL.

  Tier 1 — few-shot exemplar bank  (zero training, instant)
    Writes ``~/.acm/judge_fewshot.json`` from the hardest user corrections (cases
    where the user overrode the model). ``relevance.JudgeSuggester`` auto-loads
    this file and injects the examples into its prompt, so the judge "learns"
    yesterday's mistakes with no weight update. This is the recommended default.

  Tier 2 — DPO preference pairs  (real RLHF-lite, optional)
    Writes ``~/.acm/judge_dpo.jsonl`` as ``{prompt, chosen, rejected}`` rows
    (chosen = the user's correct verdict, rejected = the model's wrong one),
    ready for ``trl`` ``DPOTrainer`` to fine-tune a small open model you then host
    as the judge.

Usage (uv):

    uv run python backend/training/train_judge.py --dry-run
    uv run python backend/training/train_judge.py            # writes fewshot + dpo
    uv run python backend/training/train_judge.py --fewshot-only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _dataset import (  # noqa: E402
    VALID_LABELS,
    build_examples,
    label_counts,
    override_rate,
)

# Short, label-appropriate reasons so an exemplar/chosen answer reads naturally
# even though we don't store the original free-text reason.
_REASON = {
    "KEEP": "still relevant or load-bearing for the current task",
    "SUMMARIZE": "finished but worth a one-line trace",
    "DROP": "finished and unrelated to the current task",
}
# A plausible-but-wrong reason for the rejected side of a DPO pair.
_WRONG_REASON = {
    "KEEP": "looks like it might still matter",
    "SUMMARIZE": "probably safe to compress",
    "DROP": "looks done and removable",
}


def _episode_prompt(task: str, episode_text: str) -> str:
    """Single-episode framing used for DPO rows (mirrors the judge's job)."""
    return (
        "You are a context auditor. The user is CURRENTLY working on:\n"
        f"{task or '(unknown)'}\n\n"
        "Decide if this past episode is still needed. Answer KEEP, SUMMARIZE, or "
        "DROP with a short reason. Prefer KEEP when unsure.\n\n"
        f"Episode:\n{(episode_text or '')[:800]}"
    )


def _verdict_json(label: str, reason: str) -> str:
    return json.dumps({"label": label, "reason": reason})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--audits", type=Path, default=None)
    ap.add_argument("--feedback", type=Path, default=None)
    ap.add_argument("--fewshot-out", type=Path, default=Path.home() / ".acm" / "judge_fewshot.json")
    ap.add_argument("--dpo-out", type=Path, default=Path.home() / ".acm" / "judge_dpo.jsonl")
    ap.add_argument("--max-fewshot", type=int, default=12, help="exemplars to keep (balanced across labels)")
    ap.add_argument("--fewshot-only", action="store_true", help="skip the DPO export")
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    args = ap.parse_args()

    # Only gold (human-labelled) rows train the judge — no silver here.
    examples = [e for e in build_examples(audit_path=args.audits, feedback_path=args.feedback) if e.gold]
    print("Judge training data")
    print(f"  gold examples  : {len(examples)}")
    print(f"  label balance  : {label_counts(examples)}")
    print(f"  user-override  : {override_rate(examples) * 100:.1f}%")

    # Hardest cases = the user overrode the model. These teach the most.
    corrections = [
        e for e in examples
        if e.model_label in VALID_LABELS and e.label != e.model_label and (e.task or e.episode_text)
    ]
    print(f"  corrections    : {len(corrections)} (model was wrong — prime exemplars)")

    # ── Tier 1: few-shot bank (balanced across the correct labels) ───────
    fewshot = _balanced(corrections or examples, args.max_fewshot)
    fewshot_payload = {
        "version": 1,
        "examples": [
            {
                "task": (e.task or "")[:240],
                "episode_text": (e.episode_text or e.title or "")[:320],
                "label": e.label,
                "reason": _REASON.get(e.label, ""),
            }
            for e in fewshot
        ],
    }
    print(f"\nTier 1 — few-shot bank: {len(fewshot_payload['examples'])} exemplars")

    # ── Tier 2: DPO preference pairs ─────────────────────────────────────
    dpo_rows = []
    if not args.fewshot_only:
        for e in corrections:
            dpo_rows.append(
                {
                    "prompt": _episode_prompt(e.task, e.episode_text),
                    "chosen": _verdict_json(e.label, _REASON.get(e.label, "")),
                    "rejected": _verdict_json(e.model_label, _WRONG_REASON.get(e.model_label, "")),
                }
            )
        print(f"Tier 2 — DPO pairs    : {len(dpo_rows)}")

    if args.dry_run:
        print("\n[dry-run] wrote nothing. Re-run without --dry-run to emit the files.")
        if fewshot_payload["examples"]:
            print("Sample exemplar:\n  " + json.dumps(fewshot_payload["examples"][0])[:200])
        return 0

    args.fewshot_out.parent.mkdir(parents=True, exist_ok=True)
    args.fewshot_out.write_text(json.dumps(fewshot_payload, indent=2, ensure_ascii=False))
    print(f"\nWrote {args.fewshot_out}  (judge auto-loads this — Tier 1 active immediately)")

    if not args.fewshot_only:
        with args.dpo_out.open("w", encoding="utf-8") as f:
            for r in dpo_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"Wrote {args.dpo_out}  ({len(dpo_rows)} pairs for trl DPOTrainer)")
        print("\nTo run DPO (optional, needs a GPU + trl):")
        print("  uv pip install trl transformers datasets")
        print("  # load judge_dpo.jsonl as a dataset and DPO-fine-tune your base judge model")
    return 0


def _balanced(rows, cap: int):
    """Round-robin across labels so the bank isn't all DROP."""
    buckets: dict = {lbl: [] for lbl in VALID_LABELS}
    for e in rows:
        if e.label in buckets:
            buckets[e.label].append(e)
    out = []
    i = 0
    while len(out) < cap and any(buckets.values()):
        lbl = VALID_LABELS[i % len(VALID_LABELS)]
        if buckets[lbl]:
            out.append(buckets[lbl].pop(0))
        i += 1
        if i > cap * len(VALID_LABELS) + len(VALID_LABELS):
            break
    return out


if __name__ == "__main__":
    raise SystemExit(main())
