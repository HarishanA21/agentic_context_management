"""Shared helpers for the Cursor hook scripts.

Cursor invokes each hook as a subprocess: a JSON event arrives on **stdin**, and
the hook prints a JSON decision on **stdout**. The exact field names follow
Cursor's Agent Hooks schema (``hook_event_name``, ``conversation_id``,
``command``, ``file_path``, …); if Cursor renames a field, adjust the getters
here in one place.

Everything is defensive: a hook must never raise or block the agent. On any
error we emit a permissive decision and exit 0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

# Make the extension package importable so hooks reuse the same memory store the
# MCP server writes to. adapters/cursor/hooks/ -> extension/ is 3 levels up.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

try:
    from acm_mcp.memory_store import MemoryStore

    _STORE: Any = MemoryStore()
except Exception:  # degrade silently if the package isn't installed yet
    _STORE = None


def read_event() -> Dict[str, Any]:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def scope_for(event: Dict[str, Any]) -> str:
    """Group captures per conversation so memory stays per-thread."""
    cid = event.get("conversation_id") or event.get("conversationId") or "thread"
    return f"cursor:{cid}"


def remember(event: Dict[str, Any], text: str) -> None:
    if _STORE is None or not text:
        return
    try:
        _STORE.remember(text, scope=scope_for(event))
    except Exception:
        pass


def emit(decision: Dict[str, Any] | None = None) -> None:
    """Print the decision (default: allow / no-op) and exit cleanly."""
    print(json.dumps(decision or {}))
    sys.exit(0)
