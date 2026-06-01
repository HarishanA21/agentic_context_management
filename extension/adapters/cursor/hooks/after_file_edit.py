#!/usr/bin/env python3
"""Cursor afterFileEdit hook — record which files the agent touched. This is the
"file paths the agent has already touched" signal the website's summariser
prioritises; keeping it in memory lets later turns recall the working set.
"""

from __future__ import annotations

from _common import emit, read_event, remember


def main() -> None:
    event = read_event()
    path = event.get("file_path") or event.get("filePath") or ""
    edits = event.get("edits") or []
    if path:
        n = len(edits) if isinstance(edits, list) else 0
        remember(event, f"edited {path}" + (f" ({n} change(s))" if n else ""))
    emit({})


if __name__ == "__main__":
    main()
