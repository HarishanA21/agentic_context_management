#!/usr/bin/env python3
"""Cursor beforeSubmitPrompt hook — capture the user's prompt (the "every
conversation" half) into the per-conversation memory trail, then continue.

We don't rewrite the prompt here (Cursor owns its context window — that's what
the gateway is for); we only record it so `recall` and end-of-session summaries
have the thread's intents.
"""

from __future__ import annotations

from _common import emit, read_event, remember


def main() -> None:
    event = read_event()
    prompt = event.get("prompt") or event.get("text") or ""
    if isinstance(prompt, str) and prompt.strip():
        head = prompt.strip()
        remember(event, f"user: {head[:500]}")
    emit({"continue": True})


if __name__ == "__main__":
    main()
