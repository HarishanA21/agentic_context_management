#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook — inject relevant memories at the start of
a turn (the website's ``MemoryCfg.auto_view_at_start``, applied via hooks).

Claude Code runs this when the user submits a prompt. We read the local ACM
memory store and, if there are notes, prepend them as additional context the
model sees for this turn. Returning ``additionalContext`` is non-destructive —
it augments, never replaces, the user's prompt.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make the extension package importable so we reuse the same memory store the
# MCP server writes to. adapters/claude-code/hooks/ -> extension/ is 3 up.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

try:
    from acm_mcp.memory_store import MemoryStore
except Exception:
    MemoryStore = None  # degrade silently if the package isn't installed


def main() -> None:
    if MemoryStore is None:
        sys.exit(0)
    try:
        _ = json.load(sys.stdin)  # event unused for now; reserved for scoping
    except Exception:
        pass

    notes = MemoryStore().recall(limit=8)
    if not notes:
        sys.exit(0)

    context = "ACM remembered notes (auto-loaded):\n" + "\n".join(
        f"- {n}" for n in notes
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
            }
        )
    )


if __name__ == "__main__":
    main()
