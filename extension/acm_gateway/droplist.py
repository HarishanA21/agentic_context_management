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

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from .paths import DROPLIST_PATH as _DEFAULT_PATH
from .paths import SUMMARY_PATH as _SUMMARY_PATH
from .paths import atomic_write_text


def _has_image(content: Any) -> bool:
    """True when a message carries at least one image block — used to flag rows
    the UI can render inline (tool screenshots / visual-method page images)."""
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and b.get("type") in {"image", "image_url"}
            for b in content
        )
    return False


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


# Claude Code stamps `metadata.user_id` with a JSON blob that carries a
# `session_id` constant across the turns of one chat — e.g.
# ``{"device_id":"…","account_uuid":"…","session_id":"58fda58c-…"}``. We read
# that session id to *namespace* the conversation key, so every request of one
# Claude Code session (the main turn plus its auxiliary title / topic / quota
# calls, which each carry a different prefix) resolves to a single window.
#
# The regex is a fallback for a bare ``session_<uuid>`` form some clients send;
# the JSON path above is what Claude Code actually emits today.
_SESSION_RE = re.compile(r"session[_-]([0-9a-fA-F][0-9a-fA-F-]{6,})")


# Claude Code embeds the working directory in its system/env block — e.g.
# "Working directory: /Users/me/project". We read it so chats can be scoped per
# project, the same way Claude Code keys its own history by the project path.
_CWD_RE = re.compile(
    r"(?:Working directory|Current working directory|cwd)\s*[:=]\s*([^\n\r]+)", re.I
)


def project_path(messages: List[BaseMessage]) -> str:
    """Best-effort project root for a request (the cwd Claude Code embeds in its
    env block). Returns '' when not present — those windows stay unscoped."""
    for m in messages:
        if not isinstance(m, (SystemMessage, HumanMessage)):
            continue
        text = _norm_text(getattr(m, "content", ""))
        low = text.lower()
        if "directory" not in low and "cwd" not in low:
            continue
        mt = _CWD_RE.search(text)
        if mt:
            return mt.group(1).strip().strip("`'\"").rstrip("/")
    return ""


def session_namespace(body: Optional[Dict[str, Any]]) -> Optional[str]:
    """Stable per-chat namespace from the request's Claude Code metadata, or
    None when absent (then the prefix hash stands alone).

    Claude Code sends ``metadata.user_id`` as a JSON string
    (``{"device_id":…,"account_uuid":…,"session_id":"<uuid>"}``); the session id
    is constant for every request of one chat, including the auxiliary
    title/topic/quota calls. We prefer that parsed ``session_id`` and fall back
    to a ``session_<uuid>`` regex only for other client shapes."""
    if not isinstance(body, dict):
        return None
    meta = body.get("metadata")
    uid = meta.get("user_id") if isinstance(meta, dict) else None
    if not isinstance(uid, str):
        return None

    # Preferred: user_id is a JSON blob with an explicit session_id.
    sid: Optional[str] = None
    if "{" in uid:
        try:
            parsed = json.loads(uid)
            cand = parsed.get("session_id") if isinstance(parsed, dict) else None
            if isinstance(cand, str) and cand.strip():
                sid = cand.strip()
        except (json.JSONDecodeError, ValueError):
            sid = None

    # Fallback: a bare ``session_<uuid>`` segment somewhere in the string.
    if sid is None:
        m = _SESSION_RE.search(uid)
        sid = m.group(1) if m else None

    if not sid:
        return None
    return "s" + re.sub(r"[^0-9a-fA-F]", "", sid)[:24]


def conversation_key(
    messages: List[BaseMessage],
    explicit: Optional[str] = None,
    namespace: Optional[str] = None,
) -> str:
    """Identify the conversation. Prefer an explicit id from the client; else,
    when Claude Code gives us a stable session id (``namespace``), key on that
    ALONE — it uniquely and stably identifies the chat for its whole lifetime,
    so we never let the churning prefix (the date, ``<system-reminder>`` git
    block, or a rotating cache token that slips past ``_stable_for_key``) mint a
    second window for one session. Only when no session id is present do we fall
    back to hashing the settled prefix (first system + first non-system message,
    per-turn billing header stripped)."""
    if explicit:
        return explicit.strip()[:64]
    if namespace:
        ns = re.sub(r"[^A-Za-z0-9_-]", "", namespace)[:32]
        if ns:
            # One session id → one context window, full stop. The prefix is
            # deliberately excluded from the key here (see docstring).
            return ns
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
        # in-memory: conv_key -> the exact wire body last *forwarded* upstream
        # (post-pipeline: drops + trimming + summaries already applied). This is
        # "what we actually send each call" — distinct from self._seen, which is
        # the INCOMING view captured before the technique pipeline runs.
        self._sent: Dict[str, Dict[str, Any]] = {}
        # in-memory: conv_key -> stack of reversible actions (most recent last).
        # Undo is a session affordance over manual edits (drop / drop_many /
        # restore / summarize); not persisted, so a fresh gateway starts clean.
        self._undo: Dict[str, List[Dict[str, Any]]] = {}
        # in-memory: conv_key -> fingerprints of tool messages the pipeline has
        # rasterised (visual method). The `seen` rows are built from the
        # pre-pipeline (text) view, so this flags them as image-bearing on read.
        self._visualized: Dict[str, set] = {}

    # ── persistence ──────────────────────────────────────────────────────
    def _load(self) -> Dict[str, List[str]]:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        atomic_write_text(self.path, json.dumps(self._dropped, indent=2))

    def _load_summaries(self) -> Dict[str, List[str]]:
        try:
            return json.loads(self.summary_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_summaries(self) -> None:
        atomic_write_text(self.summary_path, json.dumps(self._summaries, indent=2))

    # ── summaries (episode → one injected note) ──────────────────────────
    def summaries(self, conv: str) -> List[str]:
        return list(self._summaries.get(conv, []))

    def summarize(self, conv: str, member_fps: List[str], summary: str) -> None:
        """Drop the whole episode and remember a summary note to inject in its
        place on every future request. Records a single undo entry that both
        removes the injected note and restores the members it newly dropped."""
        newly = [fp for fp in member_fps if self.drop(conv, fp)]
        bucket = self._summaries.setdefault(conv, [])
        bucket.append(summary)
        self._save_summaries()
        self.push_undo(
            conv,
            "summarize",
            f"summarize {len(member_fps)} messages",
            {"summary": summary, "fps": newly},
        )

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

    def drop(self, conv: str, fp: str) -> bool:
        """Tombstone a message. Returns True if it was newly dropped (so callers
        can record an accurate undo entry), False if it was already dropped."""
        bucket = self._dropped.setdefault(conv, [])
        if fp not in bucket:
            bucket.append(fp)
            self._save()
            return True
        return False

    def restore(self, conv: str, fp: str) -> bool:
        bucket = self._dropped.get(conv, [])
        if fp in bucket:
            bucket.remove(fp)
            if not bucket:
                self._dropped.pop(conv, None)
            self._save()
            return True
        return False

    # ── undo (session-only stack of reversible manual edits) ─────────────
    _UNDO_LIMIT = 25

    def push_undo(self, conv: str, kind: str, label: str, undo: Dict[str, Any]) -> None:
        """Record a reversible action. `undo` carries exactly what undo() needs
        to reverse it (see undo() for the per-kind shape). Capped per chat so a
        long session can't grow the stack without bound."""
        stack = self._undo.setdefault(conv, [])
        stack.append({"kind": kind, "label": label, "undo": undo})
        if len(stack) > self._UNDO_LIMIT:
            del stack[: -self._UNDO_LIMIT]

    def undo_top(self, conv: str) -> Optional[Dict[str, Any]]:
        """The label/kind of the action that undo() would reverse next, or None."""
        stack = self._undo.get(conv, [])
        if not stack:
            return None
        top = stack[-1]
        return {"kind": top["kind"], "label": top["label"], "depth": len(stack)}

    def undo(self, conv: str) -> Optional[Dict[str, Any]]:
        """Reverse the most recent recorded action. Returns a small result dict
        describing what was undone, or None if there is nothing to undo."""
        stack = self._undo.get(conv, [])
        if not stack:
            return None
        action = stack.pop()
        if not stack:
            self._undo.pop(conv, None)
        kind, u = action["kind"], action["undo"]

        if kind in ("drop", "drop_many"):
            # Reverse a removal: restore exactly the fps this action dropped
            # (only those it actually newly-dropped, tracked at record time).
            for fp in u.get("fps", []):
                self.restore(conv, fp)
        elif kind == "restore":
            # Reverse a restore: drop the fp again.
            fp = u.get("fp")
            if fp:
                self.drop(conv, fp)
        elif kind == "summarize":
            # Reverse a summarize: remove the injected note and restore its
            # members. Remove the specific note (not the whole bucket) so an
            # older summary on the same chat survives.
            note = u.get("summary")
            bucket = self._summaries.get(conv, [])
            if note in bucket:
                bucket.remove(note)
                if not bucket:
                    self._summaries.pop(conv, None)
                self._save_summaries()
            for fp in u.get("fps", []):
                self.restore(conv, fp)

        return {"kind": kind, "label": action["label"]}

    def clear_undo(self, conv: str) -> None:
        self._undo.pop(conv, None)

    # ── last-seen (for the UI) ───────────────────────────────────────────
    def record_seen(self, conv: str, messages: List[BaseMessage]) -> None:
        rows = []
        for m in messages:
            fp = fingerprint(m)
            content = getattr(m, "content", "")
            full = _norm_text(content)
            preview = full.strip().replace("\n", " ")
            rows.append(
                {
                    "fp": fp,
                    "role": _role(m),
                    "preview": (preview[:120] + "…") if len(preview) > 120 else preview,
                    "tool_call_id": getattr(m, "tool_call_id", "") or "",
                    "dropped": self.is_dropped(conv, fp),
                    # Flags a message the UI can render inline as image(s) — a
                    # tool screenshot or a visual-method rasterised page.
                    "has_image": _has_image(content),
                    # Each message's OWN size (counted once) — a ~4 chars/token
                    # estimate. Summed for the context HUD; never per-call totals.
                    "tokens": max(1, len(full) // 4) if full else 0,
                }
            )
        self._seen[conv] = {"ts": time.time(), "messages": rows}
        self._seen_msgs[conv] = list(messages)

    def seen(self, conv: str, full: bool = False) -> List[Dict[str, Any]]:
        # Recompute `dropped` against the LIVE drop-list on every read — the
        # cached rows freeze it at record time, so without this a freshly
        # dropped/restored message wouldn't reflect until the next turn (the UI
        # would look like Remove did nothing).
        rows = self._seen.get(conv, {}).get("messages", [])
        live = set(self._dropped.get(conv, []))
        viz = self._visualized.get(conv, set())
        out = [
            {
                **r,
                "dropped": r.get("fp") in live,
                "has_image": bool(r.get("has_image")) or r.get("fp") in viz,
            }
            for r in rows
        ]
        if full:
            # Attach each message's complete text so a UI can render the whole
            # conversation in order without a per-message round-trip. Rows are
            # 1:1 with the cached BaseMessage list, so we zip by index.
            msgs = self._seen_msgs.get(conv, [])
            for i, row in enumerate(out):
                if i < len(msgs):
                    row["text"] = _norm_text(getattr(msgs[i], "content", ""))
        return out

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

    # ── last-sent (the forwarded payload, for the Context Window view) ─────
    def record_sent(
        self,
        conv: str,
        *,
        surface: str,
        model: Any,
        system: Any,
        messages: Any,
        tools: Any,
    ) -> None:
        """Snapshot the exact wire body forwarded upstream for ``conv`` — the
        post-pipeline payload (drops + trimming + summaries already applied), so
        a UI can show "what we actually send each call". In-memory only;
        overwritten on every call."""
        self._sent[conv] = {
            "ts": time.time(),
            "surface": surface,
            "model": model or "",
            "system": system,
            "messages": messages or [],
            "tools": tools or [],
        }

    def sent(self, conv: str) -> Dict[str, Any]:
        """The last forwarded payload for ``conv`` (or an empty shell)."""
        snap = self._sent.get(conv)
        if not snap:
            return {
                "conversation": conv,
                "ts": 0,
                "surface": "",
                "model": "",
                "system": None,
                "messages": [],
                "tools": [],
            }
        return {"conversation": conv, **snap}

    def mark_visualized(self, conv: str, fps: List[str]) -> None:
        """Flag tool messages the pipeline rasterised this turn, so `seen` rows
        report them image-bearing even though the incoming view was text."""
        if fps:
            self._visualized.setdefault(conv, set()).update(fps)

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

    def title(self, conv: str) -> str:
        """The human-readable title for ``conv`` (first real user message)."""
        return self._title(self._seen.get(conv, {}).get("messages", []))

    def forget(self, conv: str) -> None:
        """Purge every trace of a conversation — its drop-list, summaries, and
        cached views. Used when a context window is deleted so nothing lingers."""
        dropped_changed = self._dropped.pop(conv, None) is not None
        if self._summaries.pop(conv, None) is not None:
            self._save_summaries()
        self._seen.pop(conv, None)
        self._seen_msgs.pop(conv, None)
        self._sent.pop(conv, None)
        self._undo.pop(conv, None)
        self._visualized.pop(conv, None)
        if dropped_changed:
            self._save()

    def clear_all(self) -> None:
        """Forget every conversation — drop-lists, summaries, and cached views.
        Used by the bulk reset so no captured state lingers in memory or on disk."""
        self._dropped = {}
        self._summaries = {}
        self._seen = {}
        self._seen_msgs = {}
        self._sent = {}
        self._undo = {}
        self._visualized = {}
        self._save()
        self._save_summaries()

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
