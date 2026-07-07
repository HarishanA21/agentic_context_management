"""Per-conversation request timeline for the Graph view.

Every proxied turn records one entry: the ordered message *blocks* that made
up the request (after the pipeline ran), each labelled with a within-turn diff
status (``kept``/``changed``/``removed``/``added``) and the technique that
caused any non-kept status, plus the raw pipeline events and before/after
token totals. The UI's animated timeline is rendered straight from this.

Like ``DropStore._sent`` this is in-memory only — a gateway restart starts an
empty timeline (the UI shows an empty state until the next request flows
through). A bounded ring (last :data:`TimelineStore.LIMIT` turns per
conversation) keeps memory flat; the monotonically increasing ``index``
survives ring eviction so the UI can show "#37…#87".
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from langchain_core.messages import BaseMessage

from .droplist import _norm_text, _role, fingerprint


def _block(msg: BaseMessage) -> Dict[str, Any]:
    """One timeline block: the lightweight per-message view (same estimation
    scheme as ``DropStore.record_seen`` — ~4 chars/token)."""
    text = _norm_text(getattr(msg, "content", ""))
    preview = " ".join(text.split())
    if len(preview) > 120:
        preview = preview[:120] + "…"
    return {
        "id": getattr(msg, "id", None),
        "fp": fingerprint(msg),
        "role": _role(msg),
        "tokens": max(1, len(text) // 4) if text else 1,
        "preview": preview,
        "status": "kept",
        "technique": "",
    }


def _event_verdicts(events: List[Dict[str, Any]]) -> Dict[str, tuple]:
    """Map message id -> (status, technique) from the pipeline events'
    additive ``replaced_ids`` / ``removed_ids`` / ``added_ids`` keys."""
    out: Dict[str, tuple] = {}
    for e in events or []:
        tech = str(e.get("type", ""))
        for mid in e.get("replaced_ids") or []:
            if mid:
                out[mid] = ("changed", tech)
        for mid in e.get("removed_ids") or []:
            if mid:
                out[mid] = ("removed", tech)
        for mid in e.get("added_ids") or []:
            if mid:
                out[mid] = ("added", tech)
    return out


class TimelineStore:
    """Bounded per-conversation ring of request-composition snapshots."""

    LIMIT = 50

    def __init__(self) -> None:
        # conv -> list of entry dicts, oldest first, capped at LIMIT
        self._turns: Dict[str, List[Dict[str, Any]]] = {}
        # conv -> monotonically increasing request counter
        self._counter: Dict[str, int] = {}

    def record(
        self,
        conv: str,
        *,
        surface: str,
        model: str,
        before: List[BaseMessage],
        after: List[BaseMessage],
        events: List[Dict[str, Any]],
    ) -> None:
        """Diff ``before`` (post-droplist, pre-pipeline) against ``after``
        (post-pipeline) and append one timeline entry."""
        if not conv:
            return
        verdicts = _event_verdicts(events)
        after_by_id: Dict[Optional[str], BaseMessage] = {
            getattr(m, "id", None): m for m in after
        }
        before_ids = {getattr(m, "id", None) for m in before}

        blocks: List[Dict[str, Any]] = []
        for m in before:
            b = _block(m)
            mid = b["id"]
            a = after_by_id.get(mid)
            if a is None or mid is None:
                b["status"] = "removed"
                b["technique"] = (verdicts.get(mid) or ("", ""))[1]
            else:
                after_text = _norm_text(getattr(a, "content", ""))
                if after_text != _norm_text(getattr(m, "content", "")):
                    b["status"] = "changed"
                    b["after_tokens"] = max(1, len(after_text) // 4) if after_text else 1
                    b["technique"] = (verdicts.get(mid) or ("", ""))[1]
            blocks.append(b)

        # Messages present only in `after` (e.g. the injected summary note):
        # insert them at their wire position so the row renders in send order.
        for pos, m in enumerate(after):
            mid = getattr(m, "id", None)
            if mid in before_ids and mid is not None:
                continue
            b = _block(m)
            b["status"] = "added"
            v = verdicts.get(mid)
            b["technique"] = v[1] if v else ("summarization" if pos == 0 else "")
            blocks.insert(min(pos, len(blocks)), b)

        before_tokens = sum(
            b["tokens"] for b in blocks if b["status"] != "added"
        )
        after_tokens = sum(
            b.get("after_tokens", b["tokens"])
            for b in blocks
            if b["status"] != "removed"
        )

        # Cross-turn growth: fps new since the previous entry (the blocks the
        # UI slides in at the end of the newest row).
        bucket = self._turns.setdefault(conv, [])
        prev_fps = {b["fp"] for b in bucket[-1]["blocks"]} if bucket else set()
        new_fps = [
            b["fp"]
            for b in blocks
            if b["status"] != "added" and b["fp"] not in prev_fps
        ]

        self._counter[conv] = self._counter.get(conv, 0) + 1
        bucket.append(
            {
                "index": self._counter[conv],
                "ts": time.time(),
                "surface": surface,
                "model": model or "",
                "before_tokens": before_tokens,
                "after_tokens": after_tokens,
                "new_fps": new_fps,
                "events": list(events or []),
                "blocks": blocks,
            }
        )
        del bucket[: -self.LIMIT]

    def timeline(self, conv: str, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 50), self.LIMIT))
        return list(self._turns.get(conv, []))[-limit:]

    def forget(self, conv: str) -> None:
        self._turns.pop(conv, None)
        self._counter.pop(conv, None)

    def clear_all(self) -> None:
        self._turns.clear()
        self._counter.clear()
