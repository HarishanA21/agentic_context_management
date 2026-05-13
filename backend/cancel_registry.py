"""Per-thread cancel flags.

`/chat/cancel` flips a thread's flag; agent tools (run_shell,
write_project_file, etc.) check at entry and short-circuit if it's set.
The flag is cleared when a new /chat turn begins, so it only affects the
in-flight turn.

In-process, no DB — multi-worker deployments need Redis or Postgres
LISTEN/NOTIFY to fan this out across processes, but our single-uvicorn
setup is fine.
"""

from __future__ import annotations

from threading import Lock
from typing import Set

_cancelled: Set[str] = set()
_lock = Lock()


def request_cancel(thread_id: str) -> None:
    with _lock:
        _cancelled.add(thread_id)


def is_cancelled(thread_id: str) -> bool:
    with _lock:
        return thread_id in _cancelled


def clear_cancel(thread_id: str) -> None:
    with _lock:
        _cancelled.discard(thread_id)
