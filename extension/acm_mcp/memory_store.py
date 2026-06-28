"""Tiny local-first memory store for the MCP ``remember`` / ``recall`` tools.

Local-first by design (see EXTENSION_PLAN §5 privacy): everything lives in a
single JSON file under the user's home, never leaves the machine. Scope keys let
the same store hold per-thread and per-user memories side by side, mirroring the
website's ``MemoryCfg.scope``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List

_DEFAULT_PATH = Path(
    os.getenv("ACM_MEMORY_PATH", str(Path.home() / ".acm" / "memory.json"))
)


class MemoryStore:
    def __init__(self, path: Path = _DEFAULT_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> Dict[str, List[Dict]]:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, data: Dict[str, List[Dict]]) -> None:
        self.path.write_text(json.dumps(data, indent=2))

    def remember(self, text: str, scope: str = "user") -> int:
        data = self._load()
        bucket = data.setdefault(scope, [])
        bucket.append({"text": text, "ts": time.time()})
        self._save(data)
        return len(bucket)

    def recall(self, query: str = "", scope: str = "user", limit: int = 10) -> List[str]:
        data = self._load()
        items = data.get(scope, [])
        if query:
            q = query.lower()
            items = [i for i in items if q in i.get("text", "").lower()]
        items = sorted(items, key=lambda i: i.get("ts", 0), reverse=True)[:limit]
        return [i["text"] for i in items]

    def clear(self, scope: str = "user") -> None:
        data = self._load()
        data.pop(scope, None)
        self._save(data)
