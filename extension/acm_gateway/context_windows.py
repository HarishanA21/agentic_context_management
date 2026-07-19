"""Per-chat context windows.

Each Claude Code chat is its own *context window*: a persistent entity keyed by
the conversation id (see :func:`droplist.conversation_key`) that carries its own
**technique profile** plus lifecycle metadata. A new chat auto-creates a window
that inherits the global default profile; the user can then point that one chat
at a different preset (or an inline profile body) without touching the others.
Deleting the chat deletes its window and all of its managed state.

This store owns only the *registry* — the per-window profile + title + stats +
lifecycle. The actual managed state (drop-list, summaries, snapshots) already
lives in :class:`droplist.DropStore`, keyed by the same conversation id; deleting
a window purges both.

Persisted to ``~/.acm/context_windows.json`` (file mode 0600). In-memory token
stats come from ``DropStore`` and are mirrored here on every turn so the list
still shows something after a gateway restart.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import CONTEXT_WINDOWS_PATH as _DEFAULT_PATH
from .paths import atomic_write_text

# Persisted fields with their defaults — keeps reads tolerant of older files.
_FIELDS: Dict[str, Any] = {
    "title": "",
    "project": "",          # project root (cwd) this chat belongs to, "" = unknown
    "profile_name": None,   # a preset name override (e.g. "long_chat")
    "profile_body": None,   # an inline Profile dict; wins over profile_name
    "created_at": 0.0,
    "last_seen": 0.0,
    "tokens": 0,            # last-known live context tokens (non-dropped)
    "messages": 0,          # last-known message count
    "pinned": False,
    "status": "active",     # "active" | "archived"
    # Visual-method "after enable" marker: fingerprints of tool messages that
    # existed when visual method turned on for this chat — those stay text.
    "visual_before_fps": None,
    "visual_enabled_at": 0.0,
}


def _now() -> float:
    return time.time()


def in_project(window_project: str, root: str) -> bool:
    """Does a window belong to ``root``? True when the project path matches or
    nests either way (claude launched from a sub/parent of the opened folder).
    An empty ``root`` matches everything; an unknown window project is never
    hidden (so a failed cwd parse doesn't make a chat vanish)."""
    if not root:
        return True
    if not window_project:
        return True
    wp = window_project.rstrip("/")
    r = root.rstrip("/")
    return wp == r or wp.startswith(r + os.sep) or r.startswith(wp + os.sep)


class ContextWindowStore:
    def __init__(self, path: Path = _DEFAULT_PATH) -> None:
        self.path = path
        self._data: Dict[str, Dict[str, Any]] = self._load()

    # ── persistence ──────────────────────────────────────────────────────
    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text())
            return raw if isinstance(raw, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        atomic_write_text(self.path, json.dumps(self._data, indent=2))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _row(self, conv: str) -> Dict[str, Any]:
        """The stored row for ``conv`` with any missing defaults filled in."""
        row = self._data.get(conv, {})
        return {"id": conv, **{k: row.get(k, v) for k, v in _FIELDS.items()}}

    # ── lifecycle ────────────────────────────────────────────────────────
    def ensure(self, conv: str, project: str = "") -> Dict[str, Any]:
        """Create the window on first sight (stamping ``created_at`` + project);
        else backfill the project if we now know it. Returns the row."""
        if conv not in self._data:
            row = {"created_at": _now(), "last_seen": _now()}
            if project:
                row["project"] = project
            self._data[conv] = row
            self._save()
        elif project and not self._data[conv].get("project"):
            self._data[conv]["project"] = project
            self._save()
        return self._row(conv)

    def touch(
        self,
        conv: str,
        *,
        title: Optional[str] = None,
        tokens: Optional[int] = None,
        messages: Optional[int] = None,
        project: Optional[str] = None,
    ) -> None:
        """Update last-seen + mirrored stats (and the title/project if we don't
        have one yet). Cheap; called once per turn."""
        row = self._data.setdefault(conv, {"created_at": _now()})
        row["last_seen"] = _now()
        if tokens is not None:
            row["tokens"] = int(tokens)
        if messages is not None:
            row["messages"] = int(messages)
        if project and not row.get("project"):
            row["project"] = project
        # Keep a human title: set it if empty (a user-set title is preserved by
        # the rename path).
        if title and not row.get("title"):
            row["title"] = title[:80]
        self._save()

    def delete(self, conv: str) -> bool:
        if conv in self._data:
            self._data.pop(conv, None)
            self._save()
            return True
        return False

    def clear(self) -> int:
        """Drop every context window. Returns how many were removed."""
        n = len(self._data)
        self._data = {}
        self._save()
        return n

    # ── profile (per-chat technique override) ────────────────────────────
    def set_profile(
        self, conv: str, *, name: Optional[str] = None, body: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Point this chat at a preset (``name``) or an inline profile (``body``).
        ``body`` wins over ``name``; passing neither is a no-op beyond ensuring
        the row exists."""
        row = self._data.setdefault(conv, {"created_at": _now(), "last_seen": _now()})
        if body is not None:
            row["profile_body"] = body
            row["profile_name"] = None
        elif name is not None:
            row["profile_name"] = name
            row["profile_body"] = None
        self._save()
        return self._row(conv)

    def clear_profile(self, conv: str) -> Dict[str, Any]:
        """Revert this chat to the global default profile."""
        row = self._data.get(conv)
        if row is not None:
            row.pop("profile_name", None)
            row.pop("profile_body", None)
            self._save()
        return self._row(conv)

    # ── visual-method marker ─────────────────────────────────────────────
    def set_visual_marker(self, conv: str, fps: List[str]) -> None:
        """Snapshot which tool messages predate visual method for this chat."""
        row = self._data.setdefault(conv, {"created_at": _now(), "last_seen": _now()})
        row["visual_before_fps"] = list(fps)
        row["visual_enabled_at"] = _now()
        self._save()

    def clear_visual_marker(self, conv: str) -> None:
        row = self._data.get(conv)
        if row is not None and row.get("visual_before_fps") is not None:
            row["visual_before_fps"] = None
            row["visual_enabled_at"] = 0.0
            self._save()

    def rename(self, conv: str, title: str) -> Dict[str, Any]:
        row = self._data.setdefault(conv, {"created_at": _now(), "last_seen": _now()})
        row["title"] = (title or "")[:80]
        self._save()
        return self._row(conv)

    def set_pinned(self, conv: str, pinned: bool) -> Dict[str, Any]:
        row = self._data.setdefault(conv, {"created_at": _now(), "last_seen": _now()})
        row["pinned"] = bool(pinned)
        self._save()
        return self._row(conv)

    def set_status(self, conv: str, status: str) -> Dict[str, Any]:
        row = self._data.setdefault(conv, {"created_at": _now(), "last_seen": _now()})
        row["status"] = "archived" if status == "archived" else "active"
        self._save()
        return self._row(conv)

    # ── reads ────────────────────────────────────────────────────────────
    def get(self, conv: str) -> Optional[Dict[str, Any]]:
        return self._row(conv) if conv in self._data else None

    def has_override(self, conv: str) -> bool:
        row = self._data.get(conv) or {}
        return bool(row.get("profile_body") or row.get("profile_name"))

    def list(self, project: Optional[str] = None) -> List[Dict[str, Any]]:
        """Windows, pinned first then most-recently-seen. With ``project`` set,
        only this project's chats (like Claude Code's per-project history)."""
        rows = [self._row(k) for k in self._data]
        if project:
            rows = [r for r in rows if in_project(r.get("project", ""), project)]
        return sorted(rows, key=lambda r: (r["pinned"], r["last_seen"]), reverse=True)
