"""Just-in-time retrieval primitives for the MCP server.

Mirrors the website's ``Tools/jit_tools.py`` technique — bounded retrieval so a
runaway glob or a giant file never dumps into the model's context — but operates
on the **local workspace** (the website's version reads uploaded files in S3).

Every result is hard-capped. All paths are confined to the workspace root
(``ACM_WORKSPACE`` env, else the process cwd) so the tools can't read outside it.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import List

_FIND_MAX_RESULTS = 200
_HEAD_TAIL_MAX_BYTES = 8_192
_GREP_MAX_MATCHES = 200
_SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", "dist", "build", ".next"}


def _root() -> Path:
    return Path(os.getenv("ACM_WORKSPACE", os.getcwd())).resolve()


def _safe(path: str) -> Path:
    """Resolve ``path`` under the workspace root, rejecting traversal escapes."""
    root = _root()
    p = (root / path).resolve()
    if root not in p.parents and p != root:
        raise ValueError(f"path escapes workspace root: {path}")
    return p


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            yield Path(dirpath) / fn


def find_files(pattern: str) -> str:
    """List workspace files whose name matches the glob ``pattern`` (e.g.
    ``*.py``). Returns ``path (size bytes)`` lines, capped — paths only, so the
    model can then read just what it needs."""
    root = _root()
    out: List[str] = []
    for p in _iter_files(root):
        if fnmatch.fnmatch(p.name, pattern):
            try:
                size = p.stat().st_size
            except OSError:
                continue
            out.append(f"{p.relative_to(root)} ({size} bytes)")
            if len(out) >= _FIND_MAX_RESULTS:
                out.append(f"[... capped at {_FIND_MAX_RESULTS} results ...]")
                break
    return "\n".join(out) if out else f"No files match {pattern!r}."


def read_slice(path: str, mode: str = "head", lines: int = 40) -> str:
    """Read the first (``head``) or last (``tail``) ``lines`` of a file, capped
    to ~8 KB — the JIT alternative to dumping a whole file."""
    p = _safe(path)
    if not p.is_file():
        return f"Not a file: {path}"
    try:
        data = p.read_bytes()
    except OSError as e:
        return f"Can't read {path}: {e}"
    text_lines = data.decode("utf-8", errors="replace").splitlines()
    n = max(1, int(lines))
    picked = text_lines[:n] if mode != "tail" else text_lines[-n:]
    out = "\n".join(picked)
    if len(out.encode("utf-8")) > _HEAD_TAIL_MAX_BYTES:
        out = out[: _HEAD_TAIL_MAX_BYTES // 2] + "\n\n[... truncated ...]"
    return out or "(empty)"


def grep_files(pattern: str, glob: str = "*") -> str:
    """Search workspace files (whose name matches ``glob``) for a regex
    ``pattern``. Returns ``path:line: text`` matches, capped."""
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"Bad regex: {e}"
    root = _root()
    out: List[str] = []
    for p in _iter_files(root):
        if not fnmatch.fnmatch(p.name, glob):
            continue
        try:
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        rel = p.relative_to(root)
                        out.append(f"{rel}:{i}: {line.rstrip()[:200]}")
                        if len(out) >= _GREP_MAX_MATCHES:
                            out.append(f"[... capped at {_GREP_MAX_MATCHES} matches ...]")
                            return "\n".join(out)
        except OSError:
            continue
    return "\n".join(out) if out else f"No matches for {pattern!r}."
