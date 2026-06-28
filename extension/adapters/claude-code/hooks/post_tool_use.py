#!/usr/bin/env python3
"""Claude Code PostToolUse hook — shrink big tool outputs before they enter the
context window (the "hooks" surface, EXTENSION_PLAN §method 2).

Claude Code invokes this after every tool runs, passing a JSON event on stdin
and reading our JSON decision on stdout. When a tool result is large we replace
it with a compact placeholder — the same idea as the website's
``tool_result_trimming``, applied live, per tool call.

Wire it up in settings.json (see settings.template.json):
    "PostToolUse": [{ "hooks": [{ "type": "command",
        "command": "python3 <abs path>/post_tool_use.py" }] }]

Docs: the hook may return ``{"hookSpecificOutput": {"toolResult": "..."}}`` to
override what the model sees. Keep the threshold conservative — only trim
genuinely huge outputs so we never hide a short, load-bearing result.
"""

from __future__ import annotations

import json
import sys

# Tools whose output is load-bearing and must never be trimmed (mirrors the
# website's _NEVER_TRIM_TOOLS).
NEVER_TRIM = {"execute_typescript", "memory"}
TRIGGER_CHARS = 8000  # ~2k tokens
PLACEHOLDER = "[result cleared by ACM to save context — re-run the tool if needed]"


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # malformed — do nothing, don't block the turn

    tool_name = event.get("tool_name") or event.get("toolName") or ""
    result = (
        event.get("tool_response")
        or event.get("toolResult")
        or event.get("result")
        or ""
    )
    text = result if isinstance(result, str) else json.dumps(result)

    if tool_name in NEVER_TRIM or len(text) < TRIGGER_CHARS:
        sys.exit(0)  # leave it untouched

    # Keep a short head so the model still sees the gist.
    head = text[:600]
    new_result = f"{head}\n\n{PLACEHOLDER}"
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "toolResult": new_result,
                }
            }
        )
    )


if __name__ == "__main__":
    main()
