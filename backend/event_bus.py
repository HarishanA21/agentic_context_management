"""In-memory pub/sub for live UI events.

A simple per-key fanout: callers `publish` events keyed by `thread:<id>`,
and SSE handlers `subscribe` to receive them. Subscribers get an
asyncio.Queue they can iterate over.

Lives entirely in-process — fine for the single-uvicorn-worker dev / solo
deployment we're targeting. For multi-worker scale-out, swap this module's
internals for Postgres LISTEN/NOTIFY or Redis pub/sub without changing the
public API.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator, Dict, Set


_QUEUE_MAX = 256  # per-subscriber backpressure cap


class EventBus:
    def __init__(self) -> None:
        # key -> set of asyncio.Queue. Each queue belongs to one subscriber.
        self._subs: Dict[str, Set[asyncio.Queue[dict]]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, key: str) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=_QUEUE_MAX)
        async with self._lock:
            self._subs.setdefault(key, set()).add(q)
        return q

    async def unsubscribe(self, key: str, q: asyncio.Queue[dict]) -> None:
        async with self._lock:
            subs = self._subs.get(key)
            if not subs:
                return
            subs.discard(q)
            if not subs:
                self._subs.pop(key, None)

    def publish(self, key: str, event: dict) -> None:
        """Fan an event out to all current subscribers of `key`.

        Synchronous so non-async code paths (psycopg cursor handlers, the
        autocommit helper, etc.) can call it without ceremony. If a queue
        is full we silently drop the event for that subscriber rather than
        blocking — the SSE consumer can re-fetch on reconnect.
        """
        # Stamp every event with a monotonic-ish wall clock so the UI can
        # order them if they ever arrive out of sequence.
        event = dict(event)
        event.setdefault("ts", time.time())
        subs = self._subs.get(key)
        if not subs:
            return
        for q in list(subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop oldest, push newest — keep the stream live for slow
                # subscribers but don't let them block fast ones.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass


bus = EventBus()


async def sse_stream(key: str) -> AsyncIterator[bytes]:
    """SSE-formatted async generator that yields events as they arrive.

    Designed for FastAPI's StreamingResponse with media_type='text/event-stream'.
    Sends a keepalive comment every 15 seconds so proxies / clients don't
    timeout the idle connection. Cancels cleanly when the client disconnects.
    """
    q = await bus.subscribe(key)
    try:
        # Initial event so the client knows the stream is alive.
        yield b": connected\n\n"
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                yield b": keepalive\n\n"
                continue
            payload = json.dumps(event)
            yield f"data: {payload}\n\n".encode("utf-8")
    finally:
        await bus.unsubscribe(key, q)
