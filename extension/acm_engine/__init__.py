"""Bridge to the website's context-management engine.

This is the *one* place the extension reaches into ``../backend``. It puts the
backend directory on ``sys.path`` and re-exports the **pure** technique
functions plus the ``Profile`` schema, so the gateway and the website run the
exact same code. Nothing in ``../backend`` is edited.

Why only the pure functions? ``context_editing.apply_context_edits`` is
LangGraph-specific — it calls ``agent.get_state`` / ``agent.update_state``. The
gateway has no LangGraph agent; it has a plain list of messages off the wire.
So we import the lower-level building blocks (which operate on a
``List[BaseMessage]``) and the gateway's own ``pipeline.py`` orchestrates them
in the same fixed order ``apply_context_edits`` uses.

If an import here ever fails, it's almost always because ``../backend`` moved or
a dependency in ``backend/requirements.txt`` isn't installed in this venv.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Two ways to find the engine, in priority order:
#   1. The **vendored** copy shipped inside the wheel (acm_engine/_vendor/) —
#      this is what an end user who `pip install`ed the package gets. Refresh it
#      from backend with `python scripts/sync_engine.py`.
#   2. The repo's ../backend tree — used during local dev so a checkout runs the
#      live engine without a sync step.
# Whichever wins is put on sys.path so the engine's flat imports
# (`import context_profiles`, `from context_editing import ...`) resolve.
_VENDOR = Path(__file__).resolve().parent / "_vendor"
_BACKEND = Path(__file__).resolve().parents[2] / "backend"

if (_VENDOR / "context_profiles.py").is_file():
    _ENGINE_DIR = _VENDOR
elif (_BACKEND / "context_profiles.py").is_file():
    _ENGINE_DIR = _BACKEND
else:  # pragma: no cover - defensive
    raise ImportError(
        "acm_engine found neither a vendored engine (acm_engine/_vendor/) nor "
        f"the repo backend at {_BACKEND}. Run `python scripts/sync_engine.py`."
    )

_engine_str = str(_ENGINE_DIR)
if _engine_str not in sys.path:
    sys.path.insert(0, _engine_str)

# Re-export the schema (pure pydantic, zero app coupling).
from context_profiles import (  # noqa: E402
    BUILTIN_PRESETS,
    DEFAULT_PRESET_NAME,
    PRESET_SUMMARY,
    Profile,
    parse_profile,
)

# Re-export the pure, message-list-level techniques. None of these touch the
# database, FastAPI, or a LangGraph agent — they take a list of messages and
# return rewrites.
from context_editing import (  # noqa: E402
    _DEFAULT_SUMMARY_SYSTEM as DEFAULT_SUMMARY_SYSTEM,
    evict_stale_images,
    sliding_window_trim,
    summarise_old_messages,
    trim_tool_results,
)
from cache_layout import (  # noqa: E402
    annotate_cache_breakpoints,
    read_cache_tokens,
)

# Where the engine was actually loaded from (vendored copy or repo backend).
ENGINE_DIR = _ENGINE_DIR
BACKEND_DIR = _ENGINE_DIR  # back-compat alias

__all__ = [
    "ENGINE_DIR",
    "BACKEND_DIR",
    "BUILTIN_PRESETS",
    "DEFAULT_PRESET_NAME",
    "PRESET_SUMMARY",
    "Profile",
    "parse_profile",
    "DEFAULT_SUMMARY_SYSTEM",
    "trim_tool_results",
    "summarise_old_messages",
    "sliding_window_trim",
    "evict_stale_images",
    "annotate_cache_breakpoints",
    "read_cache_tokens",
]
