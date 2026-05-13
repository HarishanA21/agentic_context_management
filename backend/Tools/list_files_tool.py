"""List files visible to the agent for the current session.

For project sessions with a sandboxed workspace attached, lists files at the
top of the workspace's `/workspace` directory. Workspace files are the live
working copy the agent is editing.

For chat sessions (no workspace), falls back to listing files uploaded to S3
under `<user_id>/<session_id>/`.

When both surfaces have content (e.g. user uploads attachments via the UI
while a workspace is also running), workspace files are listed first and S3
attachments second so the agent can distinguish "code I'm working on" from
"context the user gave me".
"""

from __future__ import annotations

import re

from langchain.tools import tool
from langchain_core.runnables import RunnableConfig

from Tools._paths import get_session_ids, get_workspace_ref
from sandbox_client import SandboxError, get_backend
from storage import get_bucket, session_prefix


def _list_workspace_files(workspace_ref: str) -> tuple[list[str], str | None]:
    """Return (lines, error). Each line is "<name> (<size> bytes)"."""
    try:
        result = get_backend().exec(
            workspace_ref,
            # find: top-level files only (-maxdepth 1 -type f), skip hidden
            # (-not -name '.*'), print "<size> <name>" lines. Sorted by name.
            "find . -maxdepth 1 -type f -not -name '.*' -printf '%s %f\\n' | sort -k2",
            cwd="/workspace",
            timeout=10,
        )
    except SandboxError as e:
        return [], f"workspace list failed: {e}"

    if not result.ok:
        return [], f"workspace list failed (exit {result.exit_code}): {result.stderr.strip()}"

    lines: list[str] = []
    for raw in result.stdout.splitlines():
        m = re.match(r"^(\d+)\s+(.+)$", raw.strip())
        if not m:
            continue
        size, name = m.group(1), m.group(2)
        lines.append(f"- {name} ({size} bytes)")
    return lines, None


def _list_s3_files(user_id: str, session_id: str) -> tuple[list[str], str | None]:
    try:
        items = get_bucket().list(session_prefix(user_id, session_id))
    except Exception as e:
        return [], f"S3 list failed: {e}"

    real = [it for it in (items or []) if it.get("id")]
    lines = []
    for it in real:
        size = (it.get("metadata") or {}).get("size", 0)
        lines.append(f"- {it.get('name')} ({size} bytes)")
    return lines, None


@tool
def list_project_files(config: RunnableConfig) -> str:
    """List the files in the current project.

    Returns each file's name and size. For project sessions with a sandboxed
    workspace, lists workspace files first (the agent's live working copy);
    user-uploaded attachments stored in S3 are listed second, under a
    separate heading.
    """
    try:
        user_id, session_id = get_session_ids(config)
    except ValueError as e:
        return f"Error: {e}"

    workspace_ref = get_workspace_ref(config)
    sections: list[str] = []

    if workspace_ref:
        ws_lines, ws_err = _list_workspace_files(workspace_ref)
        if ws_err:
            sections.append(f"Workspace files: error — {ws_err}")
        elif ws_lines:
            sections.append("Workspace files (in /workspace):")
            sections.extend(ws_lines)
        else:
            sections.append("Workspace files: none.")

    s3_lines, s3_err = _list_s3_files(user_id, session_id)
    if s3_err:
        sections.append(f"\nUploaded attachments: error — {s3_err}")
    elif s3_lines:
        if workspace_ref:
            sections.append("\nUploaded attachments (user-supplied, not in workspace):")
        sections.extend(s3_lines)
    elif not workspace_ref:
        # Chat-only sessions and no S3 files either.
        return "No files."

    return "\n".join(sections) if sections else "No files."
