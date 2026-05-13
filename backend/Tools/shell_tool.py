"""Shell tool — runs commands inside the session's sandboxed workspace.

Resolves the workspace via the runnable config (injected by /chat). The
SandboxBackend handles isolation, timeout enforcement, and PAT redaction —
this tool is a thin adapter that formats the result for the agent.
"""

from __future__ import annotations

import re

from langchain.tools import tool
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from cancel_registry import is_cancelled
from Tools._paths import get_session_mode, get_workspace_ref
from sandbox_client import SandboxError, SandboxNotFoundError, get_backend

MAX_OUTPUT_BYTES = 16_000  # cap each stream so a runaway command doesn't blow context


# Patterns that always require human approval — even in Auto mode — because
# their blast radius is wider than "edit a file in /workspace". Matched
# loosely against the command string; intentionally permissive so the agent
# can't sneak past with simple variations.
_ALWAYS_CONFIRM_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("git push to remote", re.compile(r"\bgit\s+push\b")),
    ("git force operation", re.compile(r"\bgit\s+(reset\s+--hard|push\s+(-f|--force))\b")),
    ("sudo escalation", re.compile(r"\bsudo\b")),
    ("recursive delete at root", re.compile(r"\brm\s+(-[a-z]*r[a-z]*f|-rf|-fr)\s*/[^\w]")),
    ("write outside /workspace", re.compile(r"(?:>|>>)\s*/(?!workspace/|tmp/|dev/null\b)")),
    ("network curl/wget to remote write", re.compile(r"\b(curl|wget)\b.*\s(-X\s*(POST|PUT|DELETE)|--data\b|--upload-file\b)")),
]


def _risky_reason(cmd: str) -> str | None:
    """Return a short human label if `cmd` matches an always-confirm pattern."""
    for label, pat in _ALWAYS_CONFIRM_PATTERNS:
        if pat.search(cmd):
            return label
    return None


def _truncate(s: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    if len(s) <= limit:
        return s
    head = limit // 2
    tail = limit - head
    omitted = len(s) - limit
    return f"{s[:head]}\n\n[... {omitted} bytes truncated ...]\n\n{s[-tail:]}"


@tool
def run_shell(
    cmd: str,
    config: RunnableConfig,
    cwd: str = "/workspace",
    timeout: int = 60,
) -> str:
    """Run a shell command inside the project's sandboxed workspace.

    The command is executed with `bash -c`, so pipes, redirects, and
    environment expansion all work. The working directory defaults to
    /workspace (the project's checkout root). Long output is truncated.

    Use this for: running tests, listing files, installing packages,
    inspecting the repo, anything you'd type at a terminal. Do NOT use it
    to read/write project files when read_project_file / write_project_file
    will do — those tools track the change in the project history.

    Args:
        cmd: The shell command to run.
        cwd: Working directory inside the workspace. Defaults to /workspace.
        timeout: Hard cap in seconds. Defaults to 60. Max 600.
    """
    # Cancel-aware entry: if the user clicked Stop during this turn, every
    # tool call from this point on returns this marker so the agent loop
    # unwinds cleanly. The thread_id is stable across all tool invocations
    # in one chat turn.
    thread_id = ((config or {}).get("configurable", {}) or {}).get("thread_id")
    if thread_id and is_cancelled(str(thread_id)):
        return "Cancelled by user."

    ref = get_workspace_ref(config)
    if not ref:
        return (
            "Error: this chat does not have a sandboxed workspace attached. "
            "Open or switch to a project session to enable shell access."
        )

    timeout = max(1, min(int(timeout or 60), 600))

    # Hard approval gate. Confirm mode pauses on every shell call; Auto mode
    # only pauses for patterns wide enough to escape `/workspace` (git push,
    # sudo, rm -rf /, redirects outside /workspace, network uploads).
    mode = get_session_mode(config)
    risky = _risky_reason(cmd)
    if mode == "confirm" or risky:
        decision = interrupt(
            {
                "kind": "approval_request",
                "tool": "run_shell",
                "cmd": cmd,
                "cwd": cwd,
                "timeout": timeout,
                "policy_reason": risky,  # null in confirm-mode triggered cases
            }
        )
        approved = bool(decision and decision.get("approved"))
        if not approved:
            reason = (decision or {}).get("reason") or "user denied the command"
            return f"User denied this command: {reason}"

    try:
        result = get_backend().exec(ref, cmd, cwd=cwd, timeout=timeout)
    except SandboxNotFoundError:
        return (
            "Error: the workspace for this session no longer exists "
            "(it may have been destroyed or expired). Reload the page to "
            "provision a new one."
        )
    except SandboxError as e:
        return f"Error: shell exec failed: {e}"

    parts = [f"$ {cmd}", f"(exit {result.exit_code}, {result.duration_ms} ms)"]
    if result.stdout:
        parts.append("--- stdout ---")
        parts.append(_truncate(result.stdout.rstrip()))
    if result.stderr:
        parts.append("--- stderr ---")
        parts.append(_truncate(result.stderr.rstrip()))
    if not result.stdout and not result.stderr:
        parts.append("(no output)")
    return "\n".join(parts)
