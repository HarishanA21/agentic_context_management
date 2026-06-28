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
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage

_DEFAULT_PATH = Path(
    os.getenv("ACM_DROPLIST_PATH", str(Path.home() / ".acm" / "dropped.json"))
)
_SUMMARY_PATH = Path(
    os.getenv("ACM_SUMMARY_PATH", str(Path.home() / ".acm" / "summaries.json"))
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


# Claude Code rotates a billing/cache header on every turn — e.g.
# ``x-anthropic-billing-header: cc_version=2.1.168.ed1; cc_entrypoint=cli;
# cch=c3ce6; You are Claude Code …``. Hashing it would mint a fresh
# conversation key each turn (the churning list you saw) and a Remove on one
# turn wouldn't carry to the next. We strip the volatile header so one session
# stays one conversation.
_VOLATILE_RE = re.compile(
    r"x-anthropic-billing-header:.*?(?=You are Claude Code|$)", re.S | re.I
)


def _stable_for_key(text: str) -> str:
    return _VOLATILE_RE.sub("", text)


def conversation_key(
    messages: List[BaseMessage], explicit: Optional[str] = None
) -> str:
    """Identify the conversation. Prefer an explicit id from the client; else
    hash the settled prefix (first system + first non-system message) with the
    per-turn billing header stripped, so it stays stable across a session."""
    if explicit:
        return explicit.strip()[:64]
    system = next((m for m in messages if isinstance(m, SystemMessage)), None)
    first = next((m for m in messages if not isinstance(m, SystemMessage)), None)
    basis = _stable_for_key(_norm_text(getattr(system, "content", ""))) + "␟" + (
        _norm_text(getattr(first, "content", ""))
    )
    return "c_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:14]


class DropStore:
    def __init__(
        self, path: Path = _DEFAULT_PATH, summary_path: Path = _SUMMARY_PATH
    ) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._dropped: Dict[str, List[str]] = self._load()
        # Summaries replace a whole episode: its members go on the drop-list and
        # one summary note is injected on every request. conv -> [summary text].
        self.summary_path = summary_path
        self._summaries: Dict[str, List[str]] = self._load_summaries()
        # in-memory: conv_key -> {"ts", "messages": [ {fp, role, preview, tool_call_id} ]}
        self._seen: Dict[str, Dict[str, Any]] = {}
        # in-memory: conv_key -> the full BaseMessage list last seen, so the
        # relevance auditor can re-read the real conversation on demand without
        # the gateway storing it to disk. Indices line up 1:1 with self._seen.
        self._seen_msgs: Dict[str, List[BaseMessage]] = {}

    # ── persistence ──────────────────────────────────────────────────────
    def _load(self) -> Dict[str, List[str]]:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._dropped, indent=2))

    def _load_summaries(self) -> Dict[str, List[str]]:
        try:
            return json.loads(self.summary_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_summaries(self) -> None:
        self.summary_path.write_text(json.dumps(self._summaries, indent=2))

    # ── summaries (episode → one injected note) ──────────────────────────
    def summaries(self, conv: str) -> List[str]:
        return list(self._summaries.get(conv, []))

    def summarize(self, conv: str, member_fps: List[str], summary: str) -> None:
        """Drop the whole episode and remember a summary note to inject in its
        place on every future request."""
        for fp in member_fps:
            self.drop(conv, fp)
        bucket = self._summaries.setdefault(conv, [])
        bucket.append(summary)
        self._save_summaries()

    def clear_summaries(self, conv: str) -> None:
        if self._summaries.pop(conv, None) is not None:
            self._save_summaries()

    def _inject_summaries(
        self, conv: str, messages: List[BaseMessage]
    ) -> List[BaseMessage]:
        """Insert this conversation's summary notes as SystemMessages right
        after the first system message (or at the front). No tool_calls/ids, so
        they never create orphan-cascade issues."""
        summ = self._summaries.get(conv) or []
        if not summ:
            return messages
        notes = [SystemMessage(content=s) for s in summ]
        out: List[BaseMessage] = []
        inserted = False
        for m in messages:
            out.append(m)
            if not inserted and isinstance(m, SystemMessage):
                out.extend(notes)
                inserted = True
        return out if inserted else notes + messages

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
            full = _norm_text(getattr(m, "content", ""))
            preview = full.strip().replace("\n", " ")
            rows.append(
                {
                    "fp": fp,
                    "role": _role(m),
                    "preview": (preview[:120] + "…") if len(preview) > 120 else preview,
                    "tool_call_id": getattr(m, "tool_call_id", "") or "",
                    "dropped": self.is_dropped(conv, fp),
                    # Each message's OWN size (counted once) — a ~4 chars/token
                    # estimate. Summed for the context HUD; never per-call totals.
                    "tokens": max(1, len(full) // 4) if full else 0,
                }
            )
        self._seen[conv] = {"ts": time.time(), "messages": rows}
        self._seen_msgs[conv] = list(messages)

    def seen(self, conv: str) -> List[Dict[str, Any]]:
        # Recompute `dropped` against the LIVE drop-list on every read — the
        # cached rows freeze it at record time, so without this a freshly
        # dropped/restored message wouldn't reflect until the next turn (the UI
        # would look like Remove did nothing).
        rows = self._seen.get(conv, {}).get("messages", [])
        live = set(self._dropped.get(conv, []))
        return [{**r, "dropped": r.get("fp") in live} for r in rows]

    def context_tokens(self, conv: str) -> int:
        """Estimated tokens currently in this conversation's context — the sum
        of each *non-dropped* message's own size (counted once)."""
        return sum(
            int(r.get("tokens", 0)) for r in self.seen(conv) if not r.get("dropped")
        )

    def seen_full(self, conv: str) -> List[BaseMessage]:
        """The real BaseMessage list last seen for ``conv`` — what the relevance
        auditor segments. Indices match :meth:`seen`, so an episode's member
        indices map straight onto fingerprints via :func:`fingerprint`."""
        return self._seen_msgs.get(conv, [])

    # Markers for messages that are injected context, not a real user prompt —
    # used to pick a human-readable conversation title.
    _CTX_MARKERS = (
        "<system-reminder>", "available agent types", "# claudemd",
        "x-anthropic-billing-header", "you are claude code", "<command-",
        "<local-command",
    )

    def _title(self, rows: List[Dict[str, Any]]) -> str:
        for r in rows:
            role = (r.get("role") or "").lower()
            prev = (r.get("preview") or "").strip()
            if role in ("human", "user") and not any(
                prev.lower().startswith(c) for c in self._CTX_MARKERS
            ):
                return prev[:60]
        for r in rows:  # fallback: first non-system message
            if (r.get("role") or "").lower() not in ("system", "systemmessage"):
                return (r.get("preview") or "")[:60]
        return ""

    def conversations(self) -> List[Dict[str, Any]]:
        out = []
        for k, v in self._seen.items():
            out.append(
                {
                    "key": k,
                    "title": self._title(v.get("messages", [])),
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
            return self._inject_summaries(conv, messages), 0

        removed = [m for m in messages if fingerprint(m) in drop_fps]
        if not removed:
            return self._inject_summaries(conv, messages), 0

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
        return self._inject_summaries(conv, filtered), count
