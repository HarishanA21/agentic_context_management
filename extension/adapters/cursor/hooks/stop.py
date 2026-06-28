#!/usr/bin/env python3
"""Cursor stop hook — fires when the agent finishes a turn/session.

Closes the capture→compact loop: it takes the conversation's captured trail
(prompts, shell commands, file edits collected by the other hooks) and asks the
running ``acm-gateway`` to compact it into a single carry-over summary via a real
LLM call, then replaces the raw trail with that summary so the next session's
``recall`` returns a tidy note instead of dozens of raw lines.

Degrades gracefully: if the gateway is down or has no API key, the raw trail is
left untouched (better to keep detail than lose it). Uses only the stdlib
``urllib`` so it runs under whatever ``python3`` Cursor invokes.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from _common import emit, read_event, scope_for, _STORE

_GATEWAY = os.getenv("ACM_GATEWAY_URL", "http://127.0.0.1:8807").rstrip("/")


def _compact(text: str) -> str | None:
    payload = json.dumps({"text": text, "instructions": "Cursor session trail."}).encode()
    req = urllib.request.Request(
        f"{_GATEWAY}/compact",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        return (data.get("summary") or "").strip() or None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None  # gateway down / no key / bad response — keep the raw trail


def main() -> None:
    event = read_event()
    status = event.get("status") or event.get("reason") or "done"
    stamp = time.strftime("%Y-%m-%d %H:%M")

    if _STORE is None:
        emit({})
        return

    scope = scope_for(event)
    try:
        trail = _STORE.recall(scope=scope, limit=200)
    except Exception:
        trail = []

    # Drop the marker we're about to add from the input transcript.
    transcript = "\n".join(t for t in trail if not t.startswith("[session"))
    summary = _compact(transcript) if transcript else None

    try:
        if summary:
            # Replace the raw trail with the compact summary (the real win).
            _STORE.clear(scope=scope)
            _STORE.remember(
                f"[session summary {stamp} | {status}]\n{summary}", scope=scope
            )
        else:
            # No compaction available — just leave a marker after the trail.
            _STORE.remember(f"[session stop: {status} @ {stamp}]", scope=scope)
    except Exception:
        pass

    emit({})


if __name__ == "__main__":
    main()
