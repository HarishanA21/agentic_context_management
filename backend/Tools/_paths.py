"""Path utilities shared by the project-file tools.

The /chat endpoint injects user_id and session_id into the runnable config so
file tools don't need the model to pass them. We use those to scope every
file operation to backend/uploads/<user_id>/<session_id>/.
"""

from __future__ import annotations

import os
from pathlib import Path

from langchain_core.runnables import RunnableConfig


UPLOADS_DIR = Path(os.environ.get("UPLOADS_DIR", "uploads")).resolve()


def session_dir(config: RunnableConfig) -> Path:
    """Resolve (and create) the upload dir for the current session."""
    cfg = (config or {}).get("configurable", {}) or {}
    user_id = cfg.get("user_id")
    session_id = cfg.get("session_id")
    if not user_id or not session_id:
        raise ValueError(
            "Tool called outside a /chat context — user_id/session_id missing."
        )
    d = (UPLOADS_DIR / str(user_id) / str(session_id)).resolve()
    if UPLOADS_DIR not in d.parents and d != UPLOADS_DIR:
        raise ValueError("Invalid session path.")
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_name(name: str) -> str:
    """Strip to basename and reject empty / traversal / hidden filenames."""
    base = Path(name or "").name
    if not base or base in {".", ".."} or base.startswith("."):
        raise ValueError(f"Invalid filename: {name!r}")
    return base
