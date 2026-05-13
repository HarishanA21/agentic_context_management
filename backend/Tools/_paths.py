"""Helpers shared by the project-file tools.

The /chat endpoint injects user_id and session_id into the runnable config so
file tools don't need the model to pass them. We use those to scope every
storage operation to <user_id>/<session_id>/ in the Supabase Storage bucket.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.runnables import RunnableConfig


def get_session_ids(config: RunnableConfig) -> tuple[str, str]:
    """Pull the JWT-verified user_id and session_id from the chat config."""
    cfg = (config or {}).get("configurable", {}) or {}
    user_id = cfg.get("user_id")
    session_id = cfg.get("session_id")
    if not user_id or not session_id:
        raise ValueError(
            "Tool called outside a /chat context — user_id/session_id missing."
        )
    return str(user_id), str(session_id)


def safe_name(name: str) -> str:
    """Strip to basename and reject empty / traversal / hidden filenames."""
    base = Path(name or "").name
    if not base or base in {".", ".."} or base.startswith("."):
        raise ValueError(f"Invalid filename: {name!r}")
    return base


def get_workspace_ref(config: RunnableConfig) -> str | None:
    """Return the backend_ref of the workspace attached to this /chat turn.

    The /chat endpoint injects `workspace_ref` into the runnable config when
    the session has a sandboxed workspace. Tools that need to run inside the
    workspace (shell_tool, git tools, dual-backend file tools) read it from
    here. Returns None for chat-only sessions with no workspace attached.
    """
    cfg = (config or {}).get("configurable", {}) or {}
    ref = cfg.get("workspace_ref")
    return str(ref) if ref else None


def get_session_mode(config: RunnableConfig) -> str:
    """Return the session's confirmation mode — `auto` or `confirm`.

    Used by write/exec tools to decide whether to call LangGraph's
    `interrupt()` for hard approval gates. Defaults to `auto` so a missing
    config (e.g. a tool invoked outside /chat) doesn't break things.
    """
    cfg = (config or {}).get("configurable", {}) or {}
    mode = (cfg.get("session_mode") or "auto").lower()
    return "confirm" if mode == "confirm" else "auto"
