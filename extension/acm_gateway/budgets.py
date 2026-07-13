"""Spend budgets — a daily USD cap on real, priced upstream usage.

The usage ledger (:mod:`usage`) already records the *real* cost of every proxied
turn. This module turns that into a control: a per-day dollar ceiling the user
sets, plus a switch for whether crossing it merely *warns* (soft) or *stops* new
turns with a 429 (hard). It is the individual-developer slice of the "virtual
key budget" that server-side gateways gate behind an enterprise tier — given
away free, enforced at the developer's own edge.

State is one small JSON file (``$ACM_HOME/budgets.json``), written atomically
like every other store. ``daily_usd = 0`` disables the budget entirely, so the
feature is off until the user opts in.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict

from .paths import BUDGETS_PATH, atomic_write_text


def local_day_start(now: float | None = None) -> float:
    """Unix timestamp of the most recent local midnight.

    The budget window is "today" in the user's own timezone — the same day
    boundary their calendar shows — not a rolling 24h or UTC day."""
    now = time.time() if now is None else now
    lt = time.localtime(now)
    midnight = time.struct_time(
        (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst)
    )
    return time.mktime(midnight)


class BudgetStore:
    """The user's spend cap: a daily USD ceiling + soft/hard enforcement.

    ``daily_usd``  the cap in dollars; 0 (the default) disables the budget.
    ``hard_stop``  when true, a turn that would run over the cap is refused
                   (429) instead of merely flagged.
    ``warn_frac``  fraction of the cap at which the Overview meter turns amber.
    """

    def __init__(self, path: Path = BUDGETS_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("daily_usd", 0.0)
        data.setdefault("hard_stop", False)
        data.setdefault("warn_frac", 0.8)
        return data

    def _save(self) -> None:
        try:
            atomic_write_text(self.path, json.dumps(self._data, indent=2))
        except OSError:
            pass

    def get(self) -> Dict[str, Any]:
        return dict(self._data)

    def set(
        self,
        *,
        daily_usd: float | None = None,
        hard_stop: bool | None = None,
        warn_frac: float | None = None,
    ) -> Dict[str, Any]:
        if daily_usd is not None:
            try:
                self._data["daily_usd"] = max(0.0, float(daily_usd))
            except (TypeError, ValueError):
                pass
        if hard_stop is not None:
            self._data["hard_stop"] = bool(hard_stop)
        if warn_frac is not None:
            try:
                self._data["warn_frac"] = min(max(float(warn_frac), 0.0), 1.0)
            except (TypeError, ValueError):
                pass
        self._save()
        return self.get()

    def state(self, spent_today: float) -> Dict[str, Any]:
        """The live budget slice for /cost: cap, spend, and where we stand.

        ``over`` is true once today's spend meets or passes the cap; ``over_warn``
        once it passes ``warn_frac`` of the cap. Both are false when the budget
        is disabled (cap 0), so the UI can hide the meter entirely."""
        cap = float(self._data.get("daily_usd") or 0.0)
        hard = bool(self._data.get("hard_stop"))
        warn_frac = float(self._data.get("warn_frac") or 0.8)
        if cap <= 0:
            return {
                "daily_usd": 0.0,
                "hard_stop": hard,
                "warn_frac": warn_frac,
                "spent_today": round(spent_today, 6),
                "pct": 0,
                "over": False,
                "over_warn": False,
                "enabled": False,
            }
        pct = round(spent_today / cap * 100)
        return {
            "daily_usd": round(cap, 6),
            "hard_stop": hard,
            "warn_frac": warn_frac,
            "spent_today": round(spent_today, 6),
            "pct": pct,
            "over": spent_today >= cap,
            "over_warn": spent_today >= cap * warn_frac,
            "enabled": True,
        }
