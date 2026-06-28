#!/usr/bin/env python3
"""Refresh the vendored engine copy from ../backend.

The published package must be self-contained — an end user who `pip install`s it
has no ``../backend``. So the pure technique modules are **vendored** into
``acm_engine/_vendor/`` and shipped inside the wheel. Run this whenever those
modules change in the backend, then commit the result:

    python scripts/sync_engine.py

Only the framework-light, message-list-level modules are vendored. They import
``langchain`` / ``langchain-core`` / ``pydantic`` (declared as dependencies) and
nothing from the website app (the one ``from api import _build_model`` in
``context_editing`` is lazy and only runs inside the LangGraph orchestrator,
which the gateway never calls).
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

VENDORED = [
    "context_profiles.py",
    "context_editing.py",
    "cache_layout.py",
    "relevance.py",
    "relevance_encoder.py",
]
# Framework-free subset of visual_tool the gateway's visual method needs.
VENDORED_VISUAL = ["rasterizer.py", "indexer.py"]

_HERE = Path(__file__).resolve().parent
_EXT_ROOT = _HERE.parent
_BACKEND = _EXT_ROOT.parent / "backend"
_VENDOR = _EXT_ROOT / "acm_engine" / "_vendor"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def main() -> int:
    if not _BACKEND.is_dir():
        print(f"error: backend not found at {_BACKEND}", file=sys.stderr)
        return 1
    _VENDOR.mkdir(parents=True, exist_ok=True)
    changed = 0
    targets = [(_BACKEND / n, _VENDOR / n) for n in VENDORED]
    targets += [
        (_BACKEND / "visual_tool" / n, _VENDOR / "visual_tool" / n)
        for n in VENDORED_VISUAL
    ]
    for src, dst in targets:
        name = f"{src.parent.name}/{src.name}" if src.parent.name == "visual_tool" else src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not src.is_file():
            print(f"error: missing {src}", file=sys.stderr)
            return 1
        before = _sha(dst) if dst.is_file() else "—"
        shutil.copy2(src, dst)
        after = _sha(dst)
        mark = "=" if before == after else "→"
        if before != after:
            changed += 1
        print(f"  {name}: {before} {mark} {after}")
    print(f"vendored {len(targets)} module(s), {changed} changed, into {_VENDOR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
