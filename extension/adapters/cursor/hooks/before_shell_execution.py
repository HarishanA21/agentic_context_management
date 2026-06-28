#!/usr/bin/env python3
"""Cursor beforeShellExecution hook — capture the command the agent is about to
run, then allow it. Feeds the per-conversation memory trail (the "every tool"
half of what hooks give us).

Returns ``{"permission": "allow"}``; swap to "ask"/"deny" to add a guardrail.
"""

from __future__ import annotations

from _common import emit, read_event, remember


def main() -> None:
    event = read_event()
    command = event.get("command") or event.get("commandLine") or ""
    cwd = event.get("cwd") or event.get("workspaceRoot") or ""
    if command:
        remember(event, f"$ {command}" + (f"   (cwd={cwd})" if cwd else ""))
    emit({"permission": "allow"})


if __name__ == "__main__":
    main()
