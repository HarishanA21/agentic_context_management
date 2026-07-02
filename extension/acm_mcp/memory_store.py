"""Tiny local-first memory store for the MCP ``remember`` / ``recall`` tools.

Local-first by design (see EXTENSION_PLAN §5 privacy): everything lives in a
single JSON file under the user's home, never leaves the machine. Scope keys let
the same store hold per-thread and per-user memories side by side, mirroring the
website's ``MemoryCfg.scope``.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path
from typing import Dict, List

_DEFAULT_PATH = Path(
    os.getenv("ACM_MEMORY_PATH", str(Path.home() / ".acm" / "memory.json"))
)

# Tokeniser for ranked recall: lowercase alphanumeric runs, length >= 2 so
# single-letter noise ("a", "I") doesn't drive matches.
_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> List[str]:
    return [t for t in _WORD.findall(text.lower()) if len(t) > 1]


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
        if not query.strip():
            # No query: most-recent-first, unchanged behaviour.
            items = sorted(items, key=lambda i: i.get("ts", 0), reverse=True)
            return [i["text"] for i in items[:limit]]
        ranked = self._rank(query, items)
        return [text for text, _ in ranked[:limit]]

    def _rank(self, query: str, items: List[Dict]) -> List[tuple]:
        """Rank memories against a query by IDF-weighted token overlap.

        Beats the old substring filter two ways: partial matches still surface
        (a query token need not be a contiguous substring of the memory), and
        rarer words count for more — a shared "kubernetes" outweighs a shared
        "the". A whole-query substring hit gets a boost so exact phrases still
        win, and recency breaks ties. Memories sharing no query token are
        dropped, so recall stays relevant rather than returning everything.
        """
        q_tokens = _tokens(query)
        if not q_tokens:
            return []
        q_set = set(q_tokens)

        # Document frequency across the scope, for IDF weighting.
        docs = [(_tokens(i.get("text", "")), i) for i in items]
        n = len(docs) or 1
        df: Dict[str, int] = {}
        for toks, _ in docs:
            for t in set(toks):
                df[t] = df.get(t, 0) + 1

        def idf(tok: str) -> float:
            # Smoothed IDF: always positive, so any shared token contributes.
            return math.log((n + 1) / (df.get(tok, 0) + 1)) + 1.0

        q_lower = query.lower().strip()
        scored: List[tuple] = []
        for toks, item in docs:
            tset = set(toks)
            shared = q_set & tset
            if not shared:
                continue
            score = sum(idf(t) for t in shared)
            # Coverage bonus: reward matching more of the query's distinct words.
            score *= 1.0 + len(shared) / len(q_set)
            # Exact-phrase boost so a literal substring still ranks top.
            if q_lower in item.get("text", "").lower():
                score *= 2.0
            scored.append((score, item.get("ts", 0), item["text"]))

        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        return [(text, score) for score, _, text in scored]

    def clear(self, scope: str = "user") -> None:
        data = self._load()
        data.pop(scope, None)
        self._save(data)
