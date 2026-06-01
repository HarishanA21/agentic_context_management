"""Manual message removal — the gateway "drop-list" (context tombstones).

The web app removes a message from the LangGraph checkpoint with
``RemoveMessage``. The gateway can't touch the IDE's history, but it *does* see
the full message array on every turn and rebuilds what it forwards. So we keep a
**persistent per-conversation set of message fingerprints to delete**, and strip
them from every request before the technique pipeline runs. The model then never
sees them again — full, surgical, persistent removal.

Pieces:
  * ``fingerprint(msg)``      — stable hash of role + text (+ tool_call_id), so
    the same message gets the same id turn after turn even without IDE-assigned
    ids.
  * ``conversation_key(...)`` — which conversation a request belongs to: an
    explicit id if the client sends one, else a hash of the settled prefix.
  * ``DropStore``             — persists the drop-list to ``~/.acm/dropped.json``
    and caches the last-seen messages per conversation (so a UI can list them).
  * ``DropStore.apply``       — cascade-safe filter: dropping an assistant
    tool-call also drops its tool result, and dropping a tool result strips the
    dangling call, so the forwarded request stays valid for OpenAI + Anthropic.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage

_DEFAULT_PATH = Path(
    os.getenv("ACM_DROPLIST_PATH", str(Path.home() / ".acm" / "dropped.json"))
)


def _norm_text(content: Any) -> str:
    """Flatten a message's content to plain text for hashing/preview. Image
    blocks become a compact marker so base64 never bloats the fingerprint."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for b in content:
            if isinstance(b, dict):
                t = b.get("type")
                if t == "text":
                    parts.append(str(b.get("text", "")))
                elif t in {"image", "image_url"}:
                    parts.append("[image]")
                else:
                    parts.append(f"[{t or 'block'}]")
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return str(content or "")


def _role(msg: BaseMessage) -> str:
    return getattr(msg, "type", None) or msg.__class__.__name__


def fingerprint(msg: BaseMessage) -> str:
    """Stable short id for a message: role + tool_call_id + normalised text.
    Independent of the IDE — the same content yields the same fingerprint."""
    tcid = getattr(msg, "tool_call_id", "") or ""
    # Include the names of any tool calls so two empty-content assistant stubs
    # with different calls don't collide.
    calls = ",".join(
        tc.get("id", "") for tc in (getattr(msg, "tool_calls", None) or [])
    )
    basis = f"{_role(msg)}|{tcid}|{calls}|{_norm_text(getattr(msg, 'content', ''))}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def conversation_key(
    messages: List[BaseMessage], explicit: Optional[str] = None
) -> str:
    """Identify the conversation. Prefer an explicit id from the client; else
    hash the settled prefix (first system + first non-system message), which is
    stable within a session."""
    if explicit:
        return explicit.strip()[:64]
    system = next((m for m in messages if isinstance(m, SystemMessage)), None)
    first = next((m for m in messages if not isinstance(m, SystemMessage)), None)
    basis = _norm_text(getattr(system, "content", "")) + "␟" + _norm_text(
        getattr(first, "content", "")
    )
    return "c_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:14]


class DropStore:
    def __init__(self, path: Path = _DEFAULT_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._dropped: Dict[str, List[str]] = self._load()
        # in-memory: conv_key -> {"ts", "messages": [ {fp, role, preview, tool_call_id} ]}
        self._seen: Dict[str, Dict[str, Any]] = {}

    # ── persistence ──────────────────────────────────────────────────────
    def _load(self) -> Dict[str, List[str]]:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._dropped, indent=2))

    # ── drop-list ────────────────────────────────────────────────────────
    def dropped(self, conv: str) -> List[str]:
        return list(self._dropped.get(conv, []))

    def is_dropped(self, conv: str, fp: str) -> bool:
        return fp in self._dropped.get(conv, [])

    def drop(self, conv: str, fp: str) -> None:
        bucket = self._dropped.setdefault(conv, [])
        if fp not in bucket:
            bucket.append(fp)
            self._save()

    def restore(self, conv: str, fp: str) -> bool:
        bucket = self._dropped.get(conv, [])
        if fp in bucket:
            bucket.remove(fp)
            if not bucket:
                self._dropped.pop(conv, None)
            self._save()
            return True
        return False

    # ── last-seen (for the UI) ───────────────────────────────────────────
    def record_seen(self, conv: str, messages: List[BaseMessage]) -> None:
        rows = []
        for m in messages:
            fp = fingerprint(m)
            preview = _norm_text(getattr(m, "content", "")).strip().replace("\n", " ")
            rows.append(
                {
                    "fp": fp,
                    "role": _role(m),
                    "preview": (preview[:120] + "…") if len(preview) > 120 else preview,
                    "tool_call_id": getattr(m, "tool_call_id", "") or "",
                    "dropped": self.is_dropped(conv, fp),
                }
            )
        self._seen[conv] = {"ts": time.time(), "messages": rows}

    def seen(self, conv: str) -> List[Dict[str, Any]]:
        return self._seen.get(conv, {}).get("messages", [])

    def conversations(self) -> List[Dict[str, Any]]:
        out = []
        for k, v in self._seen.items():
            out.append(
                {
                    "key": k,
                    "ts": v.get("ts", 0),
                    "count": len(v.get("messages", [])),
                    "dropped": len(self._dropped.get(k, [])),
                }
            )
        return sorted(out, key=lambda r: r["ts"], reverse=True)

    def latest_conversation(self) -> Optional[str]:
        convs = self.conversations()
        return convs[0]["key"] if convs else None

    # ── the actual removal (cascade-safe) ────────────────────────────────
    def apply(
        self, conv: str, messages: List[BaseMessage]
    ) -> Tuple[List[BaseMessage], int]:
        """Return ``(filtered, removed_count)`` with every dropped message —
        and its dependent tool-call/tool-result — stripped out."""
        drop_fps = set(self._dropped.get(conv, []))
        if not drop_fps:
            return messages, 0

        removed = [m for m in messages if fingerprint(m) in drop_fps]
        if not removed:
            return messages, 0

        # tool_call ids issued by dropped assistant messages -> drop their results
        orphan_result_for: set[str] = set()
        # tool_call ids whose RESULT was dropped -> strip that call from its AIMessage
        stripped_call_ids: set[str] = set()
        for m in removed:
            if isinstance(m, AIMessage):
                for tc in getattr(m, "tool_calls", None) or []:
                    orphan_result_for.add(tc.get("id", ""))
            if isinstance(m, ToolMessage):
                stripped_call_ids.add(getattr(m, "tool_call_id", "") or "")

        filtered: List[BaseMessage] = []
        count = 0
        for m in messages:
            fp = fingerprint(m)
            if fp in drop_fps:
                count += 1
                continue
            if (
                isinstance(m, ToolMessage)
                and (getattr(m, "tool_call_id", "") or "") in orphan_result_for
            ):
                count += 1
                continue
            if isinstance(m, AIMessage) and stripped_call_ids:
                kept_calls = [
                    tc
                    for tc in (getattr(m, "tool_calls", None) or [])
                    if tc.get("id", "") not in stripped_call_ids
                ]
                if len(kept_calls) != len(getattr(m, "tool_calls", None) or []):
                    # rebuild without the dangling calls; drop entirely if empty
                    if not kept_calls and not (getattr(m, "content", "") or ""):
                        count += 1
                        continue
                    m = AIMessage(
                        content=getattr(m, "content", ""),
                        tool_calls=kept_calls,
                        id=getattr(m, "id", None),
                    )
            filtered.append(m)
        return filtered, count
