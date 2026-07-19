"""Persistent savings ledger — the receipts for what ACM removed.

Every technique in the pipeline reports ``freed_tokens`` in its event info (see
``acm_engine`` context-editing functions). Those events are otherwise transient:
``_LAST_EVENTS`` in the gateway is in-memory and capped at 100, with no per-chat
attribution and no running total. This module durably accumulates freed tokens
per conversation and per technique so the UI can show a savings dashboard —
tokens saved, an estimated cost saved, and a per-chat breakdown — that survives
restarts.

It is deliberately additive: a monotonic counter incremented at record time. We
never re-derive it from the (lossy, capped) event log."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import SAVINGS_PATH as _DEFAULT_PATH
from .paths import atomic_write_text

# Techniques whose freed_tokens count as real context savings. cache_breakpoints
# only annotates the prefix (no output change) so it never carries freed_tokens;
# it is harmless to include but listed here for intent.
_SAVING_TYPES = {
    "visual_method",
    "tool_result_trimming",
    "image_eviction",
    "summarization",
    "sliding_window",
}


class SavingsLedger:
    """Monotonic per-conversation ledger of tokens freed by the pipeline."""

    def __init__(self, path: Path = _DEFAULT_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, Any] = self._load()

    # — persistence ————————————————————————————————————————————————
    def _load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        data.setdefault("conversations", {})
        return data

    def _save(self) -> None:
        atomic_write_text(self.path, json.dumps(self._data, indent=2))

    # — recording ——————————————————————————————————————————————————
    def record(self, conv: str, events: List[Dict[str, Any]]) -> int:
        """Add one turn's freed tokens for ``conv``. Returns tokens added.

        Idempotency is the caller's responsibility (record once per turn); the
        ledger is a pure accumulator."""
        if not conv or not events:
            return 0

        by_type: Dict[str, int] = {}
        # Per-technique before/after tokens for the Savings table (Technique |
        # Before ACM | After ACM). Visual method also keeps its dedicated
        # vis_before/vis_after pair for the token-comparison card.
        before_by_type: Dict[str, int] = {}
        after_by_type: Dict[str, int] = {}
        vis_before = 0
        vis_after = 0
        for e in events:
            t = e.get("type")
            if t not in _SAVING_TYPES:
                continue
            freed = int(e.get("freed_tokens", 0) or 0)
            if freed > 0:
                by_type[t] = by_type.get(t, 0) + freed
            bt = int(e.get("before_tokens", 0) or 0)
            at = int(e.get("after_tokens", 0) or 0)
            if bt > 0 or at > 0:
                before_by_type[t] = before_by_type.get(t, 0) + bt
                after_by_type[t] = after_by_type.get(t, 0) + at
            if t == "visual_method":
                vis_before += bt
                vis_after += at

        added = sum(by_type.values())
        if added == 0 and vis_before == 0 and not before_by_type:
            return 0

        stamp = time.time()
        conv_rec = self._data["conversations"].setdefault(
            conv,
            {"freed_tokens": 0, "turns": 0, "by_technique": {}, "first_ts": stamp},
        )
        conv_rec["freed_tokens"] += added
        conv_rec["turns"] += 1
        conv_rec["last_ts"] = stamp
        for t, n in by_type.items():
            conv_rec["by_technique"][t] = conv_rec["by_technique"].get(t, 0) + n
        if vis_before > 0:
            conv_rec["visual_before_tokens"] = (
                int(conv_rec.get("visual_before_tokens", 0)) + vis_before
            )
            conv_rec["visual_after_tokens"] = (
                int(conv_rec.get("visual_after_tokens", 0)) + vis_after
            )
        if before_by_type:
            bbt = conv_rec.setdefault("before_by_technique", {})
            abt = conv_rec.setdefault("after_by_technique", {})
            for t in before_by_type:
                bbt[t] = int(bbt.get(t, 0)) + before_by_type[t]
                abt[t] = int(abt.get(t, 0)) + after_by_type.get(t, 0)

        self._save()
        return added

    def forget(self, conv: str) -> None:
        if self._data["conversations"].pop(conv, None) is not None:
            self._save()

    def clear_all(self) -> None:
        self._data = {"conversations": {}}
        self._save()

    # — reporting ——————————————————————————————————————————————————
    def summary(
        self,
        *,
        cost_per_mtok: float = 0.0,
        titles: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """All-time totals plus a per-conversation breakdown.

        ``cost_per_mtok`` is the input price ($ per million tokens) used to turn
        freed tokens into an estimated dollar figure. ``titles`` maps conv keys
        to human titles (from the drop store) so the dashboard can label rows."""
        convs = self._data["conversations"]
        titles = titles or {}

        total_freed = sum(int(c.get("freed_tokens", 0)) for c in convs.values())
        total_turns = sum(int(c.get("turns", 0)) for c in convs.values())
        vis_before = sum(int(c.get("visual_before_tokens", 0)) for c in convs.values())
        vis_after = sum(int(c.get("visual_after_tokens", 0)) for c in convs.values())
        by_technique: Dict[str, int] = {}
        before_by_technique: Dict[str, int] = {}
        after_by_technique: Dict[str, int] = {}
        for c in convs.values():
            for t, n in c.get("by_technique", {}).items():
                by_technique[t] = by_technique.get(t, 0) + int(n)
            for t, n in c.get("before_by_technique", {}).items():
                before_by_technique[t] = before_by_technique.get(t, 0) + int(n)
            for t, n in c.get("after_by_technique", {}).items():
                after_by_technique[t] = after_by_technique.get(t, 0) + int(n)

        rows = []
        for key, c in convs.items():
            freed = int(c.get("freed_tokens", 0))
            rows.append(
                {
                    "conversation": key,
                    "title": titles.get(key, key),
                    "freed_tokens": freed,
                    "turns": int(c.get("turns", 0)),
                    "by_technique": c.get("by_technique", {}),
                    "last_ts": c.get("last_ts"),
                    "cost_saved": _cost(freed, cost_per_mtok),
                }
            )
        rows.sort(key=lambda r: r["freed_tokens"], reverse=True)

        return {
            "total_freed_tokens": total_freed,
            "total_turns": total_turns,
            "total_cost_saved": _cost(total_freed, cost_per_mtok),
            "cost_per_mtok": cost_per_mtok,
            "by_technique": by_technique,
            # With/without visual method: tool-result text tokens the model
            # would have read as text vs the refs text it reads alongside the
            # rendered images. Persisted, so it survives gateway restarts.
            "visual_method": {
                "before_tokens": vis_before,
                "after_tokens": vis_after,
                "saved_tokens": max(0, vis_before - vis_after),
                "cost_saved": _cost(max(0, vis_before - vis_after), cost_per_mtok),
            },
            # Per-technique before/after tokens for the Savings table
            # (Technique | Before ACM | After ACM). Only techniques that have
            # actually fired at least once appear here.
            "by_technique_table": [
                {
                    "technique": t,
                    "before_tokens": before_by_technique.get(t, 0),
                    "after_tokens": after_by_technique.get(t, 0),
                    "saved_tokens": max(
                        0, before_by_technique.get(t, 0) - after_by_technique.get(t, 0)
                    ),
                }
                for t in sorted(
                    set(before_by_technique) | set(after_by_technique),
                    key=lambda k: before_by_technique.get(k, 0),
                    reverse=True,
                )
            ],
            "conversations": rows,
        }


def _cost(tokens: int, per_mtok: float) -> float:
    if not per_mtok or tokens <= 0:
        return 0.0
    return round(tokens / 1_000_000 * per_mtok, 4)
