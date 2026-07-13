"""Real upstream usage capture — the anchor for the evaluation harness.

Everything else in the gateway estimates tokens at ~4 chars/token (the drop
store, the timeline, the savings ledger). That's fine for a live UI ring, but
the eval's cost/accuracy claims must rest on the **real** token counts the
provider billed. Both upstreams already return them: OpenAI-shaped responses
carry ``usage.{prompt,completion,total}_tokens`` (+ ``prompt_tokens_details.
cached_tokens``); Anthropic carries ``usage.{input,output}_tokens`` (+
``cache_read_input_tokens`` / ``cache_creation_input_tokens``).

This module (1) normalizes either shape into one dict, and (2) durably appends a
per-turn row — usage + priced cost + which techniques fired — to a JSONL-backed
ledger keyed by conversation. The eval runner reads that ledger to build its
results; the UI reads a rollup for the cost dashboard.

Like :mod:`savings` it is deliberately additive and defensive: a missing or
malformed ``usage`` degrades to zeros with ``measured=False`` and never breaks a
live turn.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import USAGE_PATH, atomic_write_text
from .pricing import PriceBook


def normalize_usage(raw: Any) -> Dict[str, Any]:
    """Map an OpenAI- or Anthropic-shaped ``usage`` block to common keys.

    Returns ``{input_tokens, output_tokens, total_tokens, cached_tokens,
    measured}``. ``measured`` is False when no usable usage was present, so the
    caller can tell a real zero from a missing one."""
    if not isinstance(raw, dict):
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                "cached_tokens": 0, "measured": False}

    def _int(*keys: str) -> int:
        for k in keys:
            v = raw.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    # OpenAI: prompt_tokens / completion_tokens. Anthropic: input_tokens /
    # output_tokens. Accept both spellings.
    inp = _int("input_tokens", "prompt_tokens")
    out = _int("output_tokens", "completion_tokens")

    # Cached input: OpenAI nests it under prompt_tokens_details; Anthropic
    # reports cache_read_input_tokens at the top level.
    cached = _int("cache_read_input_tokens")
    details = raw.get("prompt_tokens_details")
    if not cached and isinstance(details, dict):
        c = details.get("cached_tokens")
        if isinstance(c, (int, float)):
            cached = int(c)

    total = _int("total_tokens") or (inp + out)
    measured = bool(inp or out or total)
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": total,
        "cached_tokens": cached,
        "measured": measured,
    }


class UsageLedger:
    """Append-only per-turn ledger of real usage + priced cost, keyed by conv.

    One JSON file: ``{conversations: {conv: {turns: [row, ...], totals: {...}}}}``.
    Each row is one proxied turn. Totals are maintained incrementally so the UI
    rollup is O(1). The raw per-turn rows are what the eval runner consumes."""

    def __init__(self, path: Path = USAGE_PATH, prices: Optional[PriceBook] = None) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.prices = prices or PriceBook()
        self._data: Dict[str, Any] = self._load()

    # — persistence ————————————————————————————————————————————————
    def _load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {}
        data.setdefault("conversations", {})
        return data

    def _save(self) -> None:
        try:
            atomic_write_text(self.path, json.dumps(self._data, indent=2))
        except OSError:
            pass

    # — recording ——————————————————————————————————————————————————
    def record(
        self,
        conv: str,
        *,
        model: str,
        surface: str,
        raw_usage: Any,
        techniques: Optional[List[str]] = None,
        freed_tokens: int = 0,
    ) -> Dict[str, Any]:
        """Append one turn's real usage + cost. Returns the row written.

        ``techniques`` is the list of technique types that fired this turn (from
        the pipeline events) so the eval can attribute cost to an arm without a
        join. ``freed_tokens`` is the estimated context the pipeline removed —
        kept alongside the real usage for the savings-vs-cost view."""
        norm = normalize_usage(raw_usage)
        cost = self.prices.cost(model or "", norm)
        row = {
            "ts": time.time(),
            "model": model or "",
            "surface": surface,
            "techniques": techniques or [],
            "freed_tokens": int(freed_tokens or 0),
            **norm,
            **cost,
        }
        if not conv:
            return row

        rec = self._data["conversations"].setdefault(
            conv, {"turns": [], "totals": self._empty_totals()}
        )
        rec["turns"].append(row)
        t = rec["totals"]
        t["input_tokens"] += norm["input_tokens"]
        t["output_tokens"] += norm["output_tokens"]
        t["total_tokens"] += norm["total_tokens"]
        t["cached_tokens"] += norm["cached_tokens"]
        t["cost_usd"] = round(t["cost_usd"] + cost["cost_usd"], 6)
        t["turns"] += 1
        self._save()
        return row

    @staticmethod
    def _empty_totals() -> Dict[str, Any]:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                "cached_tokens": 0, "cost_usd": 0.0, "turns": 0}

    def forget(self, conv: str) -> None:
        if self._data["conversations"].pop(conv, None) is not None:
            self._save()

    def clear_all(self) -> None:
        self._data = {"conversations": {}}
        self._save()

    # — reporting ——————————————————————————————————————————————————
    def summary(self, titles: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """All-time real-usage totals + a per-conversation breakdown."""
        convs = self._data["conversations"]
        titles = titles or {}
        grand = self._empty_totals()
        rows = []
        for key, rec in convs.items():
            t = rec.get("totals", self._empty_totals())
            for f in ("input_tokens", "output_tokens", "total_tokens",
                      "cached_tokens", "turns"):
                grand[f] += int(t.get(f, 0))
            grand["cost_usd"] = round(grand["cost_usd"] + float(t.get("cost_usd", 0.0)), 6)
            rows.append({"conversation": key, "title": titles.get(key, key), **t})
        rows.sort(key=lambda r: r.get("cost_usd", 0.0), reverse=True)
        return {"totals": grand, "conversations": rows}

    def spent_since(self, since_ts: float) -> float:
        """Total priced cost of turns recorded at/after ``since_ts``.

        Walks the raw per-turn rows (not the totals, which are all-time) so the
        daily budget can ask "how much have I spent today". Cheap: a few hundred
        rows at most in a normal ledger."""
        total = 0.0
        for rec in self._data["conversations"].values():
            for row in rec.get("turns", []):
                if float(row.get("ts", 0.0)) >= since_ts:
                    total += float(row.get("cost_usd", 0.0) or 0.0)
        return round(total, 6)

    def rollup(
        self,
        *,
        titles: Optional[Dict[str, str]] = None,
        projects: Optional[Dict[str, str]] = None,
        last_ts: Optional[Dict[str, float]] = None,
        since_ts: float = 0.0,
    ) -> Dict[str, Any]:
        """Cost attribution for the Overview: headline totals, today's spend, and
        per-project + per-chat breakdowns.

        ``projects`` maps a conversation key to the project (cwd) it belongs to,
        so cost rolls up per project the way the Chats tab groups chats.
        ``titles`` / ``last_ts`` decorate the per-chat rows. ``since_ts`` is the
        start of the budget window (local midnight) for ``spent_today``."""
        convs = self._data["conversations"]
        titles = titles or {}
        projects = projects or {}
        last_ts = last_ts or {}

        grand = self._empty_totals()
        chats: List[Dict[str, Any]] = []
        by_project: Dict[str, Dict[str, Any]] = {}

        for key, rec in convs.items():
            t = rec.get("totals", self._empty_totals())
            cost = float(t.get("cost_usd", 0.0) or 0.0)
            turns = int(t.get("turns", 0) or 0)
            for f in ("input_tokens", "output_tokens", "total_tokens",
                      "cached_tokens", "turns"):
                grand[f] += int(t.get(f, 0))
            grand["cost_usd"] = round(grand["cost_usd"] + cost, 6)

            chats.append({
                "conversation": key,
                "title": titles.get(key, key),
                "project": projects.get(key, ""),
                "cost_usd": round(cost, 6),
                "total_tokens": int(t.get("total_tokens", 0)),
                "turns": turns,
                "last_ts": last_ts.get(key),
            })

            proj = projects.get(key, "") or "(unknown)"
            p = by_project.setdefault(
                proj, {"project": proj, "cost_usd": 0.0, "total_tokens": 0,
                       "turns": 0, "chats": 0}
            )
            p["cost_usd"] = round(p["cost_usd"] + cost, 6)
            p["total_tokens"] += int(t.get("total_tokens", 0))
            p["turns"] += turns
            p["chats"] += 1

        chats.sort(key=lambda r: r.get("cost_usd", 0.0), reverse=True)
        proj_rows = sorted(
            by_project.values(), key=lambda r: r.get("cost_usd", 0.0), reverse=True
        )
        return {
            "totals": grand,
            "spent_today": self.spent_since(since_ts) if since_ts else 0.0,
            "projects": proj_rows,
            "conversations": chats,
        }
