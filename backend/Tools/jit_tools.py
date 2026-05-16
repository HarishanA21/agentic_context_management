"""Just-in-time retrieval primitives.

These tools let the agent *sample* large data sources instead of bulk-
loading them — the B5 technique from CONTEXT_STRATEGIES_PLAN.md.

All four follow the same dual-backend pattern as the existing file
tools (list_files_tool, read_file_tool, write_file_tool):

  * If a sandboxed workspace is attached, shell out via the
    sandbox backend (cheap because the work happens inside the
    container; only the small result crosses the wire).
  * Otherwise fall back to scanning the user's S3 prefix in Python.

Output is hard-capped per tool so a runaway glob doesn't blow up the
chat context. ``find_files`` returns paths + sizes only; the other
three return body slices.
"""

from __future__ import annotations

import fnmatch
import io
import re
from pathlib import Path

from langchain.tools import tool
from langchain_core.runnables import RunnableConfig

from Tools._paths import get_session_ids, get_workspace_ref, safe_name
from sandbox_client import SandboxError, get_backend
from storage import file_key, get_bucket, is_not_found, session_prefix


# Caps. Per-tool because a 50KB head dump is more interesting than a
# 50KB list of file names — different ceilings make sense.
_FIND_MAX_RESULTS = 200
_HEAD_TAIL_MAX_BYTES = 8_192
_GREP_MAX_MATCHES = 200
_GREP_PER_FILE_BYTES = 200_000  # avoid pulling huge files just to scan


def _shell_quote(s: str) -> str:
    """Single-quote escape for shell — same pattern as run_shell uses."""
    return "'" + s.replace("'", "'\\''") + "'"


def _line_slice(data: bytes, n_lines: int, *, head: bool) -> str:
    """Decode `data` as UTF-8 and return the first or last `n_lines`,
    truncated to _HEAD_TAIL_MAX_BYTES of UTF-8 output."""
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return "Error: file is not UTF-8 text."
    lines = text.splitlines()
    if head:
        picked = lines[: max(1, n_lines)]
    else:
        picked = lines[-max(1, n_lines):]
    out = "\n".join(picked)
    if len(out.encode("utf-8")) > _HEAD_TAIL_MAX_BYTES:
        out = out[: _HEAD_TAIL_MAX_BYTES // 2] + "\n\n[... truncated ...]"
    return out


# ─── find_files ─────────────────────────────────────────────────────────


@tool
def find_files(pattern: str, config: RunnableConfig) -> str:
    """List project files whose name matches the glob `pattern`.

    Returns up to 200 matches, one per line, formatted as
    "path (size bytes)". Use this BEFORE read_project_file when you
    don't know the exact filename — costs almost no context budget
    compared to reading every file blindly.

    Args:
        pattern: A glob like "*.py", "test_*.md", or "config.*".
            Matches against the basename of each file. Use "*" to
            list everything.
    """
    if not pattern or not pattern.strip():
        return "Error: pattern is required (use '*' to list everything)."
    pattern = pattern.strip()

    try:
        user_id, session_id = get_session_ids(config)
    except ValueError as e:
        return f"Error: {e}"

    workspace_ref = get_workspace_ref(config)

    # Workspace branch — let find(1) do the glob; -iname for case-insensitive
    # matches feels more useful here than case-sensitive defaults.
    if workspace_ref:
        try:
            result = get_backend().exec(
                workspace_ref,
                f"find . -type f -not -path '*/\\.*' "
                f"-iname {_shell_quote(pattern)} -printf '%s %P\\n' | head -n {_FIND_MAX_RESULTS}",
                cwd="/workspace",
                timeout=15,
            )
        except SandboxError as e:
            return f"Error: workspace find failed: {e}"
        if not result.ok:
            return (
                f"Error: workspace find failed (exit {result.exit_code}): "
                f"{result.stderr.strip()[:200]}"
            )
        lines: list[str] = []
        for raw in result.stdout.splitlines():
            m = re.match(r"^(\d+)\s+(.+)$", raw.strip())
            if m:
                lines.append(f"- {m.group(2)} ({m.group(1)} bytes)")
        if not lines:
            return f"No workspace files match {pattern!r}."
        return "\n".join(lines)

    # S3 branch — list the session prefix, filter via fnmatch.
    try:
        items = get_bucket().list(session_prefix(user_id, session_id))
    except Exception as e:
        return f"Error: S3 list failed: {e}"
    real = [it for it in (items or []) if it.get("id")]
    matches: list[str] = []
    for it in real:
        name = it.get("name") or ""
        if fnmatch.fnmatch(name.lower(), pattern.lower()):
            size = (it.get("metadata") or {}).get("size", 0)
            matches.append(f"- {name} ({size} bytes)")
        if len(matches) >= _FIND_MAX_RESULTS:
            break
    if not matches:
        return f"No uploaded files match {pattern!r}."
    return "\n".join(matches)


# ─── head_file / tail_file ──────────────────────────────────────────────


def _slice_workspace_file(
    workspace_ref: str, name: str, n_lines: int, *, head: bool
) -> str:
    cmd = "head" if head else "tail"
    quoted = _shell_quote(name)
    try:
        result = get_backend().exec(
            workspace_ref,
            f"{cmd} -n {int(n_lines)} -- {quoted}",
            cwd="/workspace",
            timeout=10,
        )
    except SandboxError as e:
        return f"Error: workspace {cmd} failed: {e}"
    if not result.ok:
        # Distinguish "no such file" from other failures.
        msg = (result.stderr or "").strip()
        if "no such file" in msg.lower() or "not found" in msg.lower():
            return f"Error: '{name}' not found in workspace."
        return f"Error: workspace {cmd} failed (exit {result.exit_code}): {msg[:200]}"
    out = result.stdout or ""
    if len(out.encode("utf-8")) > _HEAD_TAIL_MAX_BYTES:
        out = out[: _HEAD_TAIL_MAX_BYTES // 2] + "\n\n[... truncated ...]"
    return out


def _slice_s3_file(
    user_id: str, session_id: str, name: str, n_lines: int, *, head: bool
) -> str:
    try:
        data = get_bucket().download(file_key(user_id, session_id, name))
    except Exception as e:
        if is_not_found(e):
            return f"Error: '{name}' not found in uploaded files."
        return f"Error: failed to download file: {e}"
    return _line_slice(data, n_lines, head=head)


def _slice_any(name: str, n_lines: int, head: bool, config) -> str:
    try:
        user_id, session_id = get_session_ids(config)
        safe = safe_name(name)
    except ValueError as e:
        return f"Error: {e}"
    workspace_ref = get_workspace_ref(config)
    if workspace_ref:
        result = _slice_workspace_file(workspace_ref, safe, n_lines, head=head)
        # If the file is *not* in the workspace, fall through to S3
        # (mirrors read_project_file's behaviour).
        if not result.startswith("Error: '"):
            return result
    return _slice_s3_file(user_id, session_id, safe, n_lines, head=head)


@tool
def head_file(filename: str, config: RunnableConfig, n_lines: int = 20) -> str:
    """Return the first N lines of a project file.

    Cheap way to peek at a file's structure (headers, imports, the
    first table row) without loading the whole thing into context.

    Args:
        filename: The basename of the file (no directory components).
        n_lines: How many lines to return. Default 20. Capped at the
            ~8KB output budget so very long lines still fit.
    """
    return _slice_any(filename, n_lines, head=True, config=config)


@tool
def tail_file(filename: str, config: RunnableConfig, n_lines: int = 20) -> str:
    """Return the last N lines of a project file.

    Useful for log files, change-log tails, or any append-only artefact
    where the most recent entries are what you want.

    Args:
        filename: The basename of the file (no directory components).
        n_lines: How many lines to return. Default 20.
    """
    return _slice_any(filename, n_lines, head=False, config=config)


# ─── grep_files ────────────────────────────────────────────────────────


def _grep_workspace(
    workspace_ref: str, pattern: str, files_glob: str | None
) -> str:
    quoted_pat = _shell_quote(pattern)
    if files_glob:
        # --include doesn't take a quoted pattern — grep's own parsing.
        include = f"--include={_shell_quote(files_glob)}"
    else:
        include = ""
    cmd = (
        f"grep -E -I -n -r --color=never {include} -e {quoted_pat} . "
        f"2>/dev/null | head -n {_GREP_MAX_MATCHES}"
    )
    try:
        result = get_backend().exec(
            workspace_ref, cmd, cwd="/workspace", timeout=20,
        )
    except SandboxError as e:
        return f"Error: workspace grep failed: {e}"
    # grep returns 1 when nothing matches — that's not a real error.
    out = (result.stdout or "").strip()
    if not out:
        return f"No workspace matches for /{pattern}/."
    return out


def _grep_s3(
    user_id: str, session_id: str, pattern: str, files_glob: str | None
) -> str:
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: invalid regex: {e}"
    try:
        items = get_bucket().list(session_prefix(user_id, session_id))
    except Exception as e:
        return f"Error: S3 list failed: {e}"
    matches: list[str] = []
    for it in items or []:
        if not it.get("id"):
            continue
        name = it.get("name") or ""
        if files_glob and not fnmatch.fnmatch(name.lower(), files_glob.lower()):
            continue
        try:
            data = get_bucket().download(file_key(user_id, session_id, name))
        except Exception:
            continue
        if len(data) > _GREP_PER_FILE_BYTES:
            data = data[:_GREP_PER_FILE_BYTES]
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                # Trim very long lines to keep the output digestible.
                shown = line if len(line) <= 240 else line[:240] + "…"
                matches.append(f"{name}:{lineno}: {shown}")
                if len(matches) >= _GREP_MAX_MATCHES:
                    break
        if len(matches) >= _GREP_MAX_MATCHES:
            break
    if not matches:
        return f"No upload matches for /{pattern}/."
    return "\n".join(matches)


@tool
def grep_files(
    pattern: str,
    config: RunnableConfig,
    files_glob: str | None = None,
) -> str:
    """Search project files for lines matching a regex.

    Returns up to 200 matching lines as `path:lineno: matched-line`.
    Use this to find usages, callers, or interesting strings without
    reading whole files.

    Args:
        pattern: An extended regex (think `grep -E`). Examples:
            "TODO", "def foo\\(", "import .*".
        files_glob: Optional glob to restrict the search to certain
            files, e.g. "*.py" or "report*.md".
    """
    if not pattern or not pattern.strip():
        return "Error: pattern is required."
    try:
        user_id, session_id = get_session_ids(config)
    except ValueError as e:
        return f"Error: {e}"
    workspace_ref = get_workspace_ref(config)
    if workspace_ref:
        ws_result = _grep_workspace(workspace_ref, pattern, files_glob)
        # If the workspace returned no matches *and* we have S3 files,
        # also scan those — uploads are often the only place the
        # interesting text lives in chat-only sessions.
        if ws_result.startswith("No workspace matches"):
            s3_result = _grep_s3(user_id, session_id, pattern, files_glob)
            if not s3_result.startswith("No upload"):
                return f"(workspace: no matches)\n\n{s3_result}"
        return ws_result
    return _grep_s3(user_id, session_id, pattern, files_glob)
