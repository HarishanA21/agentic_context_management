"""In-process event broadcaster for realtime UI updates.

The gateway edits the context window on every turn (drops / trimming /
summaries). The settings panels used to discover those edits by polling every
few seconds. Instead we publish a small event the moment a turn is recorded and
let subscribers (the VSCode host, which fans it out to its webviews over
postMessage) react immediately.

The webview itself can't open a socket — its Content-Security-Policy is
``default-src 'none'`` — so the realtime path is gateway -> Node host (SSE) ->
webview (postMessage). This module is just the gateway end: a fan-out of
asyncio queues with a bounded backlog so a slow consumer can't grow memory
without limit.

Events are plain JSON dicts. The only contract is a ``type`` field; today:
  * ``turn``   — a request flowed through the pipeline. Carries ``conv`` (the
                 chat whose context window changed) and ``project``.
  * ``window`` — a context-window lifecycle change (rename / profile / delete /
                 pin). Carries ``conv`` and the ``action``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List, Set

# Per-subscriber backlog. A turn is small; 256 is generous and still bounds a
# stalled consumer. When full we drop the oldest so the live tail wins.
_MAX_BACKLOG = 256


class _Broadcaster:
    def __init__(self) -> None:
        self._subscribers: Set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_MAX_BACKLOG)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event: Dict[str, Any]) -> None:
        """Fan an event out to every subscriber. Never blocks: a full queue
        drops its oldest event to make room for the newest."""
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - racy but safe
                    pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - racy but safe
                pass


_BUS = _Broadcaster()


def publish(event: Dict[str, Any]) -> None:
    """Publish an event to all subscribers. Safe to call from sync code; the
    queues are non-blocking."""
    _BUS.publish(event)


async def stream() -> AsyncIterator[str]:
    """Server-Sent Events stream for one subscriber. Yields SSE-framed lines,
    plus a periodic comment heartbeat so dead connections are detected and
    proxies don't time the idle socket out."""
    q = _BUS.subscribe()
    try:
        # Greet immediately so the client knows the stream is live.
        yield ": connected\n\n"
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=20.0)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            yield "data: " + json.dumps(event) + "\n\n"
    finally:
        _BUS.unsubscribe(q)


def subscriber_count() -> int:
    return len(_BUS._subscribers)
