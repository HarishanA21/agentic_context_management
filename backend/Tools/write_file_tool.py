from langchain.tools import tool
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from cancel_registry import is_cancelled
from Tools._paths import (
    get_session_ids,
    get_session_mode,
    get_workspace_ref,
    safe_name,
)
from sandbox_client import SandboxError, get_backend
from storage import file_key, get_bucket

MAX_WRITE_BYTES = 1_000_000  # 1 MB cap


# After every workspace write we run this script with $ACM_FILE set to the
# basename of the file just written. It stages the change, decides verb
# ('created' vs 'updated') from the staged status, and commits. Skips
# cleanly if there's no git repo or no actual diff (rewriting the same
# content). Token-safe: the filename is passed via env, never interpolated.
_AUTOCOMMIT_SCRIPT = r"""
set -e
cd /workspace
if [ ! -d .git ]; then echo 'no-git'; exit 0; fi
git add -- "$ACM_FILE"
status=$(git status --porcelain -- "$ACM_FILE" | head -c 2 | head -c 1)
case "$status" in
  A) verb='created' ;;
  M) verb='updated' ;;
  *) echo 'no-change'; exit 0 ;;
esac
git commit -q -m "Agent: $verb $ACM_FILE"
sha=$(git rev-parse --short HEAD)
echo "committed:$verb:$sha"
"""


def _autocommit_workspace_write(workspace_ref: str, name: str) -> tuple[str, str]:
    """Stage + commit the just-written file in /workspace.

    Returns `(short_summary, sha)`. `short_summary` is a human-readable
    string (`"committed as Agent: updated hello.py (a1b2c3d)"`) or `""` if
    nothing was committed (no-git, no-change, or commit failure). Failures
    are logged but never propagate — the write itself already succeeded.
    """
    try:
        result = get_backend().exec(
            workspace_ref,
            _AUTOCOMMIT_SCRIPT,
            cwd="/workspace",
            env={"ACM_FILE": name},
            timeout=10,
        )
    except SandboxError as e:
        print(f"[autocommit] exec failed for {name}: {e}", flush=True)
        return "", ""

    if not result.ok:
        print(
            f"[autocommit] commit failed for {name} "
            f"(exit {result.exit_code}): {result.stderr[:200]}",
            flush=True,
        )
        return "", ""

    out = result.stdout.strip()
    if not out.startswith("committed:"):
        # 'no-git' or 'no-change' — neither is an error.
        return "", ""

    parts = out.split(":")
    verb = parts[1] if len(parts) > 1 else "updated"
    sha = parts[2] if len(parts) > 2 else ""
    short = f"committed as Agent: {verb} {name}"
    if sha:
        short += f" ({sha})"
    return short, sha


@tool
def write_project_file(
    filename: str, content: str, config: RunnableConfig
) -> str:
    """Write text content to a file in the current project.

    For project sessions with a sandboxed workspace, writes to
    /workspace/<filename> — the agent's live working copy that run_shell,
    git, and tests can see. For chat sessions (no workspace), writes to S3
    under the session's prefix.

    Creates the file if missing, overwrites it if it exists.

    Args:
        filename: The basename of the file to write (no directory components).
        content: The UTF-8 text content to write.
    """
    try:
        user_id, session_id = get_session_ids(config)
        name = safe_name(filename)
    except ValueError as e:
        return f"Error: {e}"

    # Cancel-aware entry — see shell_tool.py for the same pattern.
    thread_id = ((config or {}).get("configurable", {}) or {}).get("thread_id")
    if thread_id and is_cancelled(str(thread_id)):
        return "Cancelled by user."

    if not isinstance(content, str):
        return "Error: content must be a string."

    payload = content.encode("utf-8")
    if len(payload) > MAX_WRITE_BYTES:
        return f"Error: content exceeds {MAX_WRITE_BYTES // 1024} KB limit."

    workspace_ref = get_workspace_ref(config)
    if workspace_ref:
        # Hard approval gate. In confirm mode we pause the graph BEFORE any
        # side effect (the file write). LangGraph's checkpointer persists
        # state; the UI shows the proposed edit; on resume the agent
        # re-enters this tool with the user's decision as the interrupt
        # return value.
        if get_session_mode(config) == "confirm":
            decision = interrupt(
                {
                    "kind": "approval_request",
                    "tool": "write_project_file",
                    "filename": name,
                    "size": len(payload),
                    "preview": content[:400],
                }
            )
            approved = bool(decision and decision.get("approved"))
            if not approved:
                reason = (decision or {}).get("reason") or "user denied the change"
                return f"User denied this write: {reason}"

        try:
            get_backend().write_file(workspace_ref, f"/workspace/{name}", payload)
        except SandboxError as e:
            return f"Error writing to workspace: {e}"

        # Auto-commit so the change is revertable. Best-effort: if git fails
        # for any reason the file still got written and that's the headline.
        commit_summary, _sha = _autocommit_workspace_write(workspace_ref, name)
        out = f"Wrote {len(payload)} bytes to /workspace/{name}."
        if commit_summary:
            out += f" ({commit_summary})"
        return out

    bucket = get_bucket()
    # Grab the previous content (if any) BEFORE overwriting, so we can store a
    # diff the UI can render red/green — chat-session files have no git commit.
    try:
        old_text = bucket.download(file_key(user_id, session_id, name)).decode(
            "utf-8", errors="replace"
        )
    except Exception:
        old_text = None

    try:
        bucket.upload(
            path=file_key(user_id, session_id, name),
            file=payload,
            file_options={
                "content-type": "text/plain; charset=utf-8",
                "upsert": "true",
            },
        )
    except Exception as e:
        return f"Error writing file: {e}"

    _store_s3_diff(bucket, user_id, session_id, name, old_text, content)
    return f"Wrote {len(payload)} bytes to {name}."


def _store_s3_diff(bucket, user_id, session_id, name, old_text, new_text) -> None:
    """Best-effort: store a unified diff of this write as a hidden ``.acmdiff.``
    sidecar so the UI can show a red/green diff for S3 (chat-session) files,
    which have no git commit to diff against. Never raises."""
    try:
        import difflib

        old_lines = old_text.splitlines() if old_text is not None else []
        diff_lines = list(
            difflib.unified_diff(
                old_lines,
                new_text.splitlines(),
                fromfile=name,
                tofile=name,
                lineterm="",
            )
        )
        if not diff_lines:
            return
        bucket.upload(
            path=file_key(user_id, session_id, f".acmdiff.{name}"),
            file=("\n".join(diff_lines)).encode("utf-8"),
            file_options={
                "content-type": "text/plain; charset=utf-8",
                "upsert": "true",
            },
        )
    except Exception as e:  # diff is a nicety; the write already succeeded
        print(f"[write_file] diff store failed for {name}: {e}", flush=True)
