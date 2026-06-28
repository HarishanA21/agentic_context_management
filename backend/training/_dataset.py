"""Shared dataset builder for the relevance trainers.

Two logs feed the training loop (see ``backend/relevance.py``):

  * **audits**   (``~/.acm/relevance_audits.jsonl``) — written at *suggest* time:
    the features the model saw (``task``, ``episode_text``) plus its own verdict
    (``model_label``, ``score``, ``source``).
  * **feedback** (``~/.acm/relevance_feedback.jsonl``) — written when the user
    *acts*: ``user_action`` (accept_drop / reject / …) and the ``final_label``.

This module joins them on ``(surface, conv, episode_id)`` into supervised
examples ``{task, episode_text, label, ...}`` where ``label`` is the user's
ground truth. Both ``train_encoder.py`` and ``train_judge.py`` import this so the
join logic lives in one place.

No heavy ML dependencies here — pure stdlib, so ``--dry-run`` works anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Import the engine's loaders/paths whether run from backend/ or backend/training/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from relevance import (  # noqa: E402
    _DEFAULT_AUDIT_PATH,
    _DEFAULT_FEEDBACK_PATH,
    load_audits,
    load_feedback,
)

VALID_LABELS = ("KEEP", "SUMMARIZE", "DROP")


def _conv_key(row: Dict[str, Any]) -> tuple:
    """Join key. Website logs the conversation as ``thread_id``; the gateway as
    ``conv`` — accept either so both surfaces line up."""
    conv = row.get("conv") or row.get("thread_id") or ""
    return (row.get("surface") or "", conv, row.get("episode_id") or "")


def _final_label_from_feedback(fb: Dict[str, Any]) -> Optional[str]:
    """Resolve the user's ground-truth label from a feedback row."""
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


class Example:
    __slots__ = ("task", "episode_text", "label", "model_label", "source", "conv", "gold", "title")

    def __init__(self, **kw: Any) -> None:
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def as_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__slots__}


def build_examples(
    *,
    audit_path: Optional[Path] = None,
    feedback_path: Optional[Path] = None,
    include_model_labels: bool = False,
) -> List[Example]:
    """Return supervised examples.

    Each feedback row with a resolvable ``final_label`` is joined to its audit
    row (the most recent audit for that key at-or-before the feedback) to recover
    the ``task`` + ``episode_text`` the model judged. These are the **gold**
    examples (``gold=True``).

    With ``include_model_labels`` the audit rows that never received feedback are
    added as **silver** examples (``gold=False``) carrying the model's own
    ``model_label`` — the "distill from the judge" cold-start, useful before
    enough human corrections exist.
    """
    audits = load_audits(audit_path or _DEFAULT_AUDIT_PATH)
    feedback = load_feedback(feedback_path or _DEFAULT_FEEDBACK_PATH)

    # key -> audit rows sorted by ts (latest last)
    by_key: Dict[tuple, List[Dict[str, Any]]] = {}
    for a in audits:
        by_key.setdefault(_conv_key(a), []).append(a)
    for rows in by_key.values():
        rows.sort(key=lambda r: r.get("ts", 0))

    used_keys: set = set()
    out: List[Example] = []

    for fb in feedback:
        label = _final_label_from_feedback(fb)
        if label is None:
            continue
        key = _conv_key(fb)
        audit_rows = by_key.get(key)
        if not audit_rows:
            # No feature row (e.g. feedback logged before audits existed). Fall
            # back to the title carried on the feedback row so it isn't wasted.
            out.append(
                Example(
                    task="", episode_text=str(fb.get("title") or ""),
                    label=label, model_label=(fb.get("shown_label") or "").upper(),
                    source=fb.get("source"), conv=key[1], gold=True,
                    title=fb.get("title"),
                )
            )
            continue
        ts = fb.get("ts", float("inf"))
        match = None
        for a in audit_rows:
            if a.get("ts", 0) <= ts:
                match = a
        match = match or audit_rows[-1]
        used_keys.add(key)
        out.append(
            Example(
                task=match.get("task", ""), episode_text=match.get("episode_text", ""),
                label=label, model_label=(match.get("model_label") or "").upper(),
                source=match.get("source"), conv=key[1], gold=True,
                title=match.get("title"),
            )
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
                Example(
                    task=a.get("task", ""), episode_text=a.get("episode_text", ""),
                    label=ml, model_label=ml, source=a.get("source"),
                    conv=key[1], gold=False, title=a.get("title"),
                )
            )
    return out


def label_counts(examples: List[Example]) -> Dict[str, int]:
    counts = {lbl: 0 for lbl in VALID_LABELS}
    for ex in examples:
        if ex.label in counts:
            counts[ex.label] += 1
    return counts


def override_rate(examples: List[Example]) -> float:
    """Fraction of gold examples where the user overrode the model's label —
    the signal the trainers most want to learn from."""
    gold = [e for e in examples if e.gold and e.model_label in VALID_LABELS]
    if not gold:
        return 0.0
    overridden = sum(1 for e in gold if e.label != e.model_label)
    return overridden / len(gold)


def group_holdout(examples: List[Example], frac: float = 0.2) -> tuple:
    """Split by conversation so no thread leaks across train/eval. Returns
    ``(train, eval)``."""
    convs = sorted({e.conv for e in examples})
    n_eval = max(1, int(len(convs) * frac)) if len(convs) > 1 else 0
    eval_convs = set(convs[:n_eval])
    train = [e for e in examples if e.conv not in eval_convs]
    ev = [e for e in examples if e.conv in eval_convs]
    return train, ev
