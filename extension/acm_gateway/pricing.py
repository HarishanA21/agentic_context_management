"""OpenRouter price book — turn a real ``usage`` object into a dollar figure.

The savings ledger (:mod:`savings`) reports *freed* tokens against a single flat
``ACM_COST_PER_MTOK``. That's fine for a rough "look how much we saved" number,
but the evaluation harness needs the **real** cost of each turn: the actual
input/output/cached token split the provider billed, priced at that specific
model's per-token rate. OpenRouter publishes every model's price in its public
``/models`` endpoint, so we fetch it once, cache it on disk, and price usage
objects from it.

Design:
  * **Offline-first.** A bundled fallback table (the models in EVALUATION_PLAN.md
    §8) means pricing works with no network — important for the eval runner and
    for tests. A successful fetch overlays the fallback and is cached to disk.
  * **Cached prices are prices, not usage.** OpenRouter reports prices in **$ per
    token** (not per Mtok); we keep that unit internally and only convert for
    display. ``cached`` input, when the provider reports it, is priced at the
    model's cache-read rate when known, else at the input rate (conservative —
    never under-counts).
  * **Never raises.** An unknown model prices to ``0.0`` with ``priced=False`` so
    a missing entry degrades to "we couldn't price this" rather than crashing a
    live turn.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from .paths import PRICING_CACHE_PATH, atomic_write_text

# Per-TOKEN prices (USD). Sourced from OpenRouter's live /models on 2026-07-08.
# Kept as the offline fallback so the eval works with no network. A live fetch
# overlays these. cache_read is the discounted repeated-input rate where known.
_FALLBACK: Dict[str, Dict[str, float]] = {
    # Paid anchor.
    "google/gemini-2.5-flash": {"prompt": 0.30e-6, "completion": 2.50e-6, "cache_read": 0.075e-6},
    # Free multimodal lane (priced at 0 — but listed so priced=True).
    "google/gemma-4-31b-it:free": {"prompt": 0.0, "completion": 0.0},
    "google/gemma-4-26b-a4b-it:free": {"prompt": 0.0, "completion": 0.0},
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free": {"prompt": 0.0, "completion": 0.0},
    "nvidia/nemotron-nano-12b-v2-vl:free": {"prompt": 0.0, "completion": 0.0},
    # Optional U4 specialist.
    "bytedance/ui-tars-1.5-7b": {"prompt": 0.10e-6, "completion": 0.20e-6},
}

_MODELS_URL = "https://openrouter.ai/api/v1/models"
_CACHE_TTL = 24 * 3600  # refetch at most once a day


class PriceBook:
    """Model → per-token price, with a disk cache and an offline fallback."""

    def __init__(self, path=PRICING_CACHE_PATH) -> None:
        self.path = path
        self._prices: Dict[str, Dict[str, float]] = dict(_FALLBACK)
        self._fetched_ts: float = 0.0
        self._load_cache()

    # — persistence ————————————————————————————————————————————————
    def _load_cache(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        prices = data.get("prices")
        if isinstance(prices, dict):
            # Cache overlays fallback (live data wins, fallback fills gaps).
            self._prices = {**_FALLBACK, **prices}
            self._fetched_ts = float(data.get("fetched_ts", 0) or 0)

    def _save_cache(self) -> None:
        try:
            atomic_write_text(
                self.path,
                json.dumps({"fetched_ts": self._fetched_ts, "prices": self._prices}, indent=2),
            )
        except OSError:
            pass  # a price cache that can't persist still works in-memory

    # — refresh ————————————————————————————————————————————————————
    def refresh(self, *, force: bool = False, now: Optional[float] = None) -> bool:
        """Fetch live prices from OpenRouter. Returns True if updated.

        Best-effort: any failure leaves the current (cache or fallback) table
        intact and returns False. ``now`` is injectable for tests."""
        now = time.time() if now is None else now
        if not force and (now - self._fetched_ts) < _CACHE_TTL:
            return False
        try:
            import httpx

            resp = httpx.get(_MODELS_URL, timeout=httpx.Timeout(30.0))
            resp.raise_for_status()
            models = resp.json().get("data", [])
        except Exception:
            return False

        fetched: Dict[str, Dict[str, float]] = {}
        for m in models:
            mid = m.get("id")
            p = m.get("pricing") or {}
            if not mid:
                continue
            entry: Dict[str, float] = {}
            for src, dst in (("prompt", "prompt"), ("completion", "completion"),
                             ("input_cache_read", "cache_read")):
                try:
                    entry[dst] = float(p.get(src, "") or 0)
                except (TypeError, ValueError):
                    continue
            if "prompt" in entry:
                fetched[mid] = entry
        if not fetched:
            return False
        self._prices = {**_FALLBACK, **fetched}
        self._fetched_ts = now
        self._save_cache()
        return True

    # — pricing ————————————————————————————————————————————————————
    def price(self, model: str) -> Optional[Dict[str, float]]:
        """Per-token price dict for ``model`` (exact, then base id sans ``:tag``)."""
        if model in self._prices:
            return self._prices[model]
        base = model.split(":", 1)[0] if model else model
        return self._prices.get(base)

    def cost(self, model: str, usage: Dict[str, int]) -> Dict[str, Any]:
        """Cost of one ``usage`` object at ``model``'s rate.

        ``usage`` uses the normalized keys from :mod:`usage` — ``input_tokens``,
        ``output_tokens``, ``cached_tokens``. Cached tokens are billed at the
        cache-read rate when known and, to avoid double counting, are subtracted
        from the input tokens billed at the full prompt rate."""
        p = self.price(model)
        inp = int(usage.get("input_tokens", 0) or 0)
        out = int(usage.get("output_tokens", 0) or 0)
        cached = int(usage.get("cached_tokens", 0) or 0)
        if p is None:
            return {"cost_usd": 0.0, "priced": False}

        cache_rate = p.get("cache_read", p.get("prompt", 0.0))
        fresh_in = max(0, inp - cached)
        cost = (
            fresh_in * p.get("prompt", 0.0)
            + cached * cache_rate
            + out * p.get("completion", 0.0)
        )
        return {"cost_usd": round(cost, 6), "priced": True}
