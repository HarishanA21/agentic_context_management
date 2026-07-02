"""Training-data export — turn the relevance feedback + audit logs into the
files the two trainers actually read.

The pruning UI already writes two JSONL logs (via ``acm_engine``):

  * **audits**   (``~/.acm/relevance_audits.jsonl``) — one row per suggestion at
    *suggest* time: the features the model saw (``task``, ``episode_text``) plus
    its own verdict (``model_label``, ``score``, ``source``).
  * **feedback** (``~/.acm/relevance_feedback.jsonl``) — one row per user
    decision: ``user_action`` (accept_drop / reject / …) and ``final_label``.

They join on ``(surface, conv, episode_id)`` into supervised examples whose
label is the user's ground truth. This mirrors ``backend/training/_dataset.py``
exactly — but that module lives outside the gateway's dependency boundary (a
pip-installed wheel ships only ``acm_engine/_vendor``, not ``../backend``), so
the join is re-implemented here on top of the engine's exposed loaders. Keeping
the key + label rules identical is the contract; ``test_training_export`` guards
it.

Two output shapes, matching MODEL_TRAINING_PLAN.md:

  * **encoder** — ``{task, episode_text, label, model_label, gold, ...}`` rows,
    the relevance-encoder training set (Phase 1).
  * **judge_dpo** — ``{prompt, chosen, rejected}`` preference pairs built from
    real user overrides (Phase 3): ``chosen`` = the user's true label, and each
    of the other two labels becomes a ``rejected``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from acm_engine import load_audits, load_feedback

VALID_LABELS = ("KEEP", "SUMMARIZE", "DROP")


def _conv_key(row: Dict[str, Any]) -> Tuple[str, str, str]:
    """Join key. The website logs the conversation as ``thread_id``; the gateway
    as ``conv`` — accept either so both surfaces line up."""
    conv = row.get("conv") or row.get("thread_id") or ""
    return (row.get("surface") or "", conv, row.get("episode_id") or "")


def _final_label_from_feedback(fb: Dict[str, Any]) -> Optional[str]:
    """The user's ground-truth label from a feedback row — same rules as
    ``backend/training/_dataset.py``."""
    fl = (fb.get("final_label") or "").upper()
    if fl in VALID_LABELS:
        return fl
    action = (fb.get("user_action") or "").lower()
    if action == "accept_drop":
        shown = (fb.get("shown_label") or "DROP").upper()
        return shown if shown in VALID_LABELS else "DROP"
    if action in ("reject", "keep", "restore"):
        return "KEEP"
    return None


def build_encoder_rows(
    *,
    audit_path: Optional[Path] = None,
    feedback_path: Optional[Path] = None,
    include_model_labels: bool = False,
) -> List[Dict[str, Any]]:
    """Join feedback onto its audit features. Each feedback row with a resolvable
    ``final_label`` becomes a **gold** example. With ``include_model_labels`` the
    audit rows that never got feedback are added as **silver** (``gold=False``)
    rows carrying the model's own label — the cold-start "distill from the judge"
    set, useful before enough human corrections exist."""
    audits = load_audits(audit_path)
    feedback = load_feedback(feedback_path)

    # key -> audit rows sorted by ts (latest last).
    by_key: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for a in audits:
        by_key.setdefault(_conv_key(a), []).append(a)
    for rows in by_key.values():
        rows.sort(key=lambda r: r.get("ts", 0))

    used_keys: set = set()
    out: List[Dict[str, Any]] = []

    for fb in feedback:
        label = _final_label_from_feedback(fb)
        if label is None:
            continue
        key = _conv_key(fb)
        audit_rows = by_key.get(key)
        if not audit_rows:
            # Feedback logged before any audit — keep it on the title so the
            # correction isn't wasted, even without episode features.
            out.append(
                {
                    "task": "",
                    "episode_text": str(fb.get("title") or ""),
                    "label": label,
                    "model_label": (fb.get("shown_label") or "").upper(),
                    "source": fb.get("source"),
                    "conv": key[1],
                    "gold": True,
                    "title": fb.get("title"),
                }
            )
            continue
        # Most recent audit at-or-before the feedback ts.
        ts = fb.get("ts", float("inf"))
        match = None
        for a in audit_rows:
            if a.get("ts", 0) <= ts:
                match = a
        match = match or audit_rows[-1]
        used_keys.add(key)
        out.append(
            {
                "task": match.get("task", ""),
                "episode_text": match.get("episode_text", ""),
                "label": label,
                "model_label": (match.get("model_label") or "").upper(),
                "source": match.get("source"),
                "conv": key[1],
                "gold": True,
                "title": match.get("title"),
            }
        )

    if include_model_labels:
        for key, rows in by_key.items():
            if key in used_keys:
                continue
            a = rows[-1]
            ml = (a.get("model_label") or "").upper()
            if ml not in VALID_LABELS:
                continue
            out.append(
                {
                    "task": a.get("task", ""),
                    "episode_text": a.get("episode_text", ""),
                    "label": ml,
                    "model_label": ml,
                    "source": a.get("source"),
                    "conv": key[1],
                    "gold": False,
                    "title": a.get("title"),
                }
            )
    return out


def _judge_prompt(task: str, episode_text: str) -> str:
    """Single-episode framing the judge trainer scores. Kept byte-identical to
    ``backend/training/train_judge._episode_prompt`` so exported pairs are
    drop-in for DPO — ``test_training_export`` guards the wording."""
    return (
        "You are a context auditor. The user is CURRENTLY working on:\n"
        f"{task or '(unknown)'}\n\n"
        "Decide if this past episode is still needed. Answer KEEP, SUMMARIZE, or "
        "DROP with a short reason. Prefer KEEP when unsure.\n\n"
        f"Episode:\n{(episode_text or '')[:800]}"
    )


def build_judge_pairs(
    *,
    audit_path: Optional[Path] = None,
    feedback_path: Optional[Path] = None,
) -> List[Dict[str, str]]:
    """DPO preference pairs from gold examples that have real episode features.
    ``chosen`` = the user's true label; each other valid label becomes a
    ``rejected``, so one correction yields up to two pairs."""
    rows = build_encoder_rows(
        audit_path=audit_path, feedback_path=feedback_path, include_model_labels=False
    )
    pairs: List[Dict[str, str]] = []
    for r in rows:
        if not r.get("gold") or not r.get("episode_text"):
            continue
        chosen = r["label"]
        if chosen not in VALID_LABELS:
            continue
        prompt = _judge_prompt(r.get("task", ""), r["episode_text"])
        for other in VALID_LABELS:
            if other != chosen:
                pairs.append({"prompt": prompt, "chosen": chosen, "rejected": other})
    return pairs


def label_counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {lbl: 0 for lbl in VALID_LABELS}
    for r in rows:
        if r.get("label") in counts:
            counts[r["label"]] += 1
    return counts


def override_rate(rows: List[Dict[str, Any]]) -> float:
    """Fraction of gold rows where the user overrode the model — the signal the
    trainers most want. 0.0 when there are no comparable rows."""
    gold = [r for r in rows if r.get("gold") and r.get("model_label") in VALID_LABELS]
    if not gold:
        return 0.0
    overridden = sum(1 for r in gold if r["label"] != r["model_label"])
    return round(overridden / len(gold), 4)


def summary(*, include_model_labels: bool = False) -> Dict[str, Any]:
    """Counts only — a cheap health check for the UI before exporting."""
    rows = build_encoder_rows(include_model_labels=include_model_labels)
    gold = [r for r in rows if r.get("gold")]
    pairs = build_judge_pairs()
    return {
        "encoder_examples": len(rows),
        "gold_examples": len(gold),
        "silver_examples": len(rows) - len(gold),
        "judge_pairs": len(pairs),
        "label_counts": label_counts(rows),
        "override_rate": override_rate(rows),
    }


def export(
    dest_dir: Path,
    *,
    include_model_labels: bool = False,
) -> Dict[str, Any]:
    """Write ``encoder.jsonl`` + ``judge_dpo.jsonl`` into ``dest_dir`` and return
    a manifest (paths + counts). The trainers read these files directly."""
    dest_dir = Path(dest_dir).expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)

    encoder_rows = build_encoder_rows(include_model_labels=include_model_labels)
    judge_pairs = build_judge_pairs()

    enc_path = dest_dir / "encoder.jsonl"
    judge_path = dest_dir / "judge_dpo.jsonl"
    _write_jsonl(enc_path, encoder_rows)
    _write_jsonl(judge_path, judge_pairs)

    gold = [r for r in encoder_rows if r.get("gold")]
    return {
        "ok": True,
        "dir": str(dest_dir),
        "encoder_path": str(enc_path),
        "judge_path": str(judge_path),
        "encoder_examples": len(encoder_rows),
        "gold_examples": len(gold),
        "silver_examples": len(encoder_rows) - len(gold),
        "judge_pairs": len(judge_pairs),
        "label_counts": label_counts(encoder_rows),
        "override_rate": override_rate(encoder_rows),
    }


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
