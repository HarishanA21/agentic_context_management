"""Per-thread described-names registry for ts_code_mode.

When the agent calls ``describe_tools(["foo", "bar"])``, those names
become *sticky* for the rest of the LangGraph thread — execute_typescript
will only build shims for names this thread has described, and
subsequent turns don't have to re-describe them.

We deliberately keep this in process memory rather than the LangGraph
checkpoint state for v1:
  * The agent rebuilds on MCP toggles / model swaps anyway, which
    invalidates the cache by changing the thread context.
  * Storing into the checkpoint state would require a custom
    AgentState class — a bigger refactor than this feature warrants.
  * Backend restarts cause the registry to forget, and the model
    naturally re-describes on its next call. The cost is one extra
    tool call per restart, which is fine.

Concurrency note: we use a single asyncio.Lock for the whole map.
The critical sections are O(1) (set ops) so contention is moot at
realistic load.
"""

from __future__ import annotations

import asyncio
from typing import Iterable, Optional


_registry: dict[str, set[str]] = {}
_lock = asyncio.Lock()


def thread_id_from_config(config: Optional[dict]) -> str:
    """Recover the per-thread key the rest of the app uses.

    Matches what backend/api.py puts in ``config['configurable']`` for
    every /chat invocation: ``f"{user_id}:{session_id}"``. If we can't
    find one (unit test, malformed config), fall back to a literal
    so the registry still works in isolation.
    """
    if not config:
        return "_anon"
    configurable = (config or {}).get("configurable") or {}
    tid = configurable.get("thread_id") or configurable.get("session_id")
    return str(tid or "_anon")


async def registry_add(thread_id: str, names: Iterable[str]) -> set[str]:
    """Merge ``names`` into the thread's registry; return the new set."""
    async with _lock:
        bucket = _registry.setdefault(thread_id, set())
        for n in names:
            if n:
                bucket.add(n)
        return set(bucket)


async def registry_get(thread_id: str) -> set[str]:
    """Read-only snapshot of the thread's described-names set."""
    async with _lock:
        return set(_registry.get(thread_id) or ())


async def registry_clear(thread_id: str) -> None:
    """Drop the entry — used by tests and when an agent rebuild kicks
    everyone off. Safe to call on a missing thread_id.
    """
    async with _lock:
        _registry.pop(thread_id, None)
