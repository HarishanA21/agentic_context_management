"""Memory tool — agent-owned scratchpad backed by S3.

Implements the six-command protocol Anthropic ships under
`memory_20250818` (view, create, str_replace, insert, delete, rename)
as a plain LangChain tool. Provider-agnostic: the model invokes it
the same way it invokes any other tool, regardless of which provider
(OpenAI, Anthropic, OpenRouter, …) the user has wired up.

Scope (set on the context-management profile):
  - "thread" (default): keys live under memory/<user>__<thread>/...
    so two chats by the same user don't see each other's notes.
  - "user":            memory/<user>/... — notes shared across all the
    user's chats. Use for cross-session knowledge that should persist
    a year later.

Secret-scrub: writes containing what look like API keys are refused.
The check is intentionally cautious — better a false-positive than
leaking a token into durable storage.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field


_MEMORY_PREFIX = "memory"
_MAX_FILE_BYTES = 64 * 1024  # individual notes capped at 64 KB
_MAX_FILES_PER_SCOPE = 200  # belt + braces on accidental fan-out

# Conservative: 32+ chars of mixed alphanumerics with at least one
# digit and one letter. Catches OpenAI / Anthropic / Slack / GitHub
# tokens in one regex without flagging plain code identifiers.
_HIGH_ENTROPY_RE = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9_\-]{31,}|[A-Z0-9]{20,})"
)


# ─── path normalisation ────────────────────────────────────────────────


def _normalise_path(raw: Optional[str]) -> str:
    """Coerce model-supplied paths into a safe relative path under the
    scope prefix. Rejects empty / traversal / absolute drive paths."""
    p = (raw or "").strip()
    if not p:
        return ""
    # Strip the synthetic "/memories" prefix Anthropic's cookbook uses.
    if p.startswith("/memories/"):
        p = p[len("/memories/"):]
    elif p == "/memories":
        return ""  # root listing
    p = p.lstrip("/")
    # Disallow .. traversal anywhere in the path.
    parts = [seg for seg in p.split("/") if seg]
    for seg in parts:
        if seg in {".", ".."} or "\x00" in seg or seg.startswith("."):
            raise ValueError(f"invalid path segment: {seg!r}")
    return "/".join(parts)


def _scope_id_from_config(config: Optional[Dict[str, Any]], scope: str) -> str:
    cfg = (config or {}).get("configurable", {}) or {}
    user_id = str(cfg.get("user_id") or "").strip()
    if not user_id:
        raise ValueError("memory tool: no user_id in config")
    if scope == "user":
        return user_id
    thread_id = str(cfg.get("thread_id") or "").strip()
    if not thread_id:
        # Fall back to user scope rather than error — keeps the tool
        # useful in odd contexts (subagents, tests).
        return user_id
    # ':' shows up in our thread ids (`{user}:{session}`). Convert
    # since some S3 backends are fussier about it than others.
    return f"{user_id}__{thread_id.replace(':', '_')}"


def _abs_key(scope_id: str, rel_path: str) -> str:
    rel_path = rel_path.strip("/")
    return f"{_MEMORY_PREFIX}/{scope_id}/{rel_path}" if rel_path else f"{_MEMORY_PREFIX}/{scope_id}"


# ─── handler (one instance per call; cheap) ────────────────────────────


class _MemoryHandler:
    def __init__(self, scope_id: str):
        self.scope_id = scope_id
        # Lazy-import to avoid a hard dep on storage at module load.
        from storage import get_bucket, is_not_found

        self._bucket = get_bucket()
        self._is_not_found = is_not_found

    # ── reads ──────────────────────────────────────────────────────────

    def view(self, rel_path: str) -> str:
        """Directory listing or file body, mirroring the Anthropic spec."""
        prefix = _abs_key(self.scope_id, rel_path) + "/"
        # First, attempt a list — if results are non-empty it's a dir.
        try:
            items = self._bucket.list(_abs_key(self.scope_id, rel_path))
        except Exception as e:
            return f"Error: view list failed: {e}"

        if items:
            lines = [f"Directory /{rel_path or 'memories'}/:"]
            for it in items:
                size = (it.get("metadata") or {}).get("size", 0)
                lines.append(f"- {it.get('name')} ({size} bytes)")
            return "\n".join(lines)

        # Otherwise, try to read it as a file.
        if not rel_path:
            return "Directory /memories/ is empty."
        try:
            data = self._bucket.download(_abs_key(self.scope_id, rel_path))
        except Exception as e:
            if self._is_not_found(e):
                return f"Error: /memories/{rel_path} not found."
            return f"Error: view download failed: {e}"
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return "Error: file is not UTF-8 text."

    # ── writes ─────────────────────────────────────────────────────────

    def create(self, rel_path: str, file_text: str) -> str:
        if not rel_path:
            return "Error: create requires a path."
        if file_text is None:
            return "Error: create requires file_text."
        body = file_text.encode("utf-8")
        if len(body) > _MAX_FILE_BYTES:
            return f"Error: file_text exceeds {_MAX_FILE_BYTES} bytes."
        if _looks_like_secret(file_text):
            return (
                "Error: refusing to save — file_text looks like it contains "
                "an API key or other high-entropy secret. Scrub it first."
            )
        # Cap the total number of files per scope.
        try:
            existing = self._bucket.list(_abs_key(self.scope_id, ""))
        except Exception:
            existing = []
        if len(existing) >= _MAX_FILES_PER_SCOPE:
            return (
                f"Error: memory cap reached ({_MAX_FILES_PER_SCOPE} files). "
                "Delete or rename old notes first."
            )
        try:
            self._bucket.upload(
                _abs_key(self.scope_id, rel_path),
                body,
                file_options={"content-type": "text/plain; charset=utf-8"},
            )
        except Exception as e:
            return f"Error: create upload failed: {e}"
        return f"Created /memories/{rel_path} ({len(body)} bytes)."

    def str_replace(self, rel_path: str, old_str: str, new_str: str) -> str:
        if not rel_path or old_str is None or new_str is None:
            return "Error: str_replace requires path, old_str, new_str."
        try:
            data = self._bucket.download(_abs_key(self.scope_id, rel_path))
        except Exception as e:
            if self._is_not_found(e):
                return f"Error: /memories/{rel_path} not found."
            return f"Error: str_replace download failed: {e}"
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return "Error: file is not UTF-8 text."
        count = text.count(old_str)
        if count == 0:
            return "Error: old_str not found in file."
        if count > 1:
            return (
                f"Error: old_str matches {count} times — add surrounding "
                "context so it's unique."
            )
        new_text = text.replace(old_str, new_str, 1)
        if _looks_like_secret(new_str):
            return "Error: refusing to save — new_str looks like a secret."
        body = new_text.encode("utf-8")
        if len(body) > _MAX_FILE_BYTES:
            return f"Error: result would exceed {_MAX_FILE_BYTES} bytes."
        try:
            self._bucket.upload(
                _abs_key(self.scope_id, rel_path),
                body,
                file_options={"content-type": "text/plain; charset=utf-8"},
            )
        except Exception as e:
            return f"Error: str_replace upload failed: {e}"
        return f"Updated /memories/{rel_path} ({len(body)} bytes)."

    def insert(self, rel_path: str, insert_line: int, new_str: str) -> str:
        if not rel_path or new_str is None:
            return "Error: insert requires path and new_str."
        try:
            data = self._bucket.download(_abs_key(self.scope_id, rel_path))
        except Exception as e:
            if self._is_not_found(e):
                return f"Error: /memories/{rel_path} not found."
            return f"Error: insert download failed: {e}"
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return "Error: file is not UTF-8 text."
        if _looks_like_secret(new_str):
            return "Error: refusing to save — new_str looks like a secret."
        lines = text.splitlines()
        n = max(0, min(int(insert_line), len(lines)))
        lines.insert(n, new_str)
        body = ("\n".join(lines)).encode("utf-8")
        if len(body) > _MAX_FILE_BYTES:
            return f"Error: result would exceed {_MAX_FILE_BYTES} bytes."
        try:
            self._bucket.upload(
                _abs_key(self.scope_id, rel_path),
                body,
                file_options={"content-type": "text/plain; charset=utf-8"},
            )
        except Exception as e:
            return f"Error: insert upload failed: {e}"
        return f"Inserted into /memories/{rel_path} at line {n}."

    def delete(self, rel_path: str) -> str:
        if not rel_path:
            return "Error: delete requires a path."
        try:
            self._bucket.remove([_abs_key(self.scope_id, rel_path)])
        except Exception as e:
            return f"Error: delete failed: {e}"
        return f"Deleted /memories/{rel_path}."

    def rename(self, old_path: str, new_path: str) -> str:
        if not old_path or not new_path:
            return "Error: rename requires old_path and new_path."
        try:
            data = self._bucket.download(_abs_key(self.scope_id, old_path))
        except Exception as e:
            if self._is_not_found(e):
                return f"Error: /memories/{old_path} not found."
            return f"Error: rename download failed: {e}"
        try:
            self._bucket.upload(
                _abs_key(self.scope_id, new_path),
                data,
                file_options={"content-type": "text/plain; charset=utf-8"},
            )
            self._bucket.remove([_abs_key(self.scope_id, old_path)])
        except Exception as e:
            return f"Error: rename failed mid-way: {e}"
        return f"Renamed /memories/{old_path} → /memories/{new_path}."


def _looks_like_secret(text: str) -> bool:
    """True if any line contains a long mixed-charset run that
    plausibly belongs to an API key."""
    if not text:
        return False
    for line in text.splitlines():
        m = _HIGH_ENTROPY_RE.search(line)
        if not m:
            continue
        chunk = m.group(0)
        has_digit = any(c.isdigit() for c in chunk)
        has_letter = any(c.isalpha() for c in chunk)
        if has_digit and has_letter:
            return True
    return False


# ─── LangChain tool surface ────────────────────────────────────────────


_MEMORY_DESCRIPTION = (
    "Persistent scratchpad / notes. Use this BEFORE you answer to recall "
    "what you wrote in earlier turns, and BEFORE long context gets "
    "summarised so important state survives. Commands: view(path), "
    "create(path, file_text), str_replace(path, old_str, new_str), "
    "insert(path, insert_line, new_str), delete(path), rename(old_path, new_path). "
    "Paths look like '/memories/findings.md'. Notes scoped to this thread by "
    "default."
)


class MemoryInput(BaseModel):
    command: str = Field(
        description="One of: view, create, str_replace, insert, delete, rename.",
    )
    path: Optional[str] = Field(default=None, description="Used by view/create/str_replace/insert/delete.")
    file_text: Optional[str] = Field(default=None, description="Body for `create`.")
    old_str: Optional[str] = Field(default=None, description="Match for `str_replace`. Must be unique.")
    new_str: Optional[str] = Field(default=None, description="Replacement / insert text.")
    insert_line: Optional[int] = Field(default=None, description="Zero-based line index for `insert`.")
    old_path: Optional[str] = Field(default=None, description="Source for `rename`.")
    new_path: Optional[str] = Field(default=None, description="Destination for `rename`.")


def _maybe_log(
    scope_id: str,
    config: Optional[Dict[str, Any]],
    edit_type: str,
    details: Dict[str, Any],
) -> None:
    """Best-effort context_events log so the UI's applied-edits panel
    shows memory activity. Never raises into the tool's return path."""
    try:
        from api import _record_context_event, app  # lazy

        cfg = (config or {}).get("configurable", {}) or {}
        user_id = str(cfg.get("user_id") or "")
        session_id = str(cfg.get("session_id") or "")
        thread_id = str(cfg.get("thread_id") or session_id)
        if not (user_id and session_id and thread_id):
            return
        with app.state.pool.connection() as conn:
            _record_context_event(
                conn,
                user_id=user_id,
                session_id=session_id,
                thread_id=thread_id,
                turn_index=0,  # the orchestrator stamps real turn_index; tool calls don't track it
                edit_type=edit_type,
                freed_tokens=0,
                details={"scope_id": scope_id, **details},
            )
    except Exception:
        pass


def make_memory_tool(scope: str = "thread") -> StructuredTool:
    """Build the LangChain tool. `scope` decides whether two chats by
    the same user share notes (`user`) or each has its own (`thread`).
    """
    if scope not in {"thread", "user"}:
        scope = "thread"

    async def _run(
        command: str,
        config: RunnableConfig,
        path: Optional[str] = None,
        file_text: Optional[str] = None,
        old_str: Optional[str] = None,
        new_str: Optional[str] = None,
        insert_line: Optional[int] = None,
        old_path: Optional[str] = None,
        new_path: Optional[str] = None,
    ) -> str:
        try:
            scope_id = _scope_id_from_config(config, scope)
        except ValueError as e:
            return f"Error: {e}"

        try:
            rel = _normalise_path(path)
            rel_old = _normalise_path(old_path) if old_path else ""
            rel_new = _normalise_path(new_path) if new_path else ""
        except ValueError as e:
            return f"Error: {e}"

        handler = _MemoryHandler(scope_id)
        cmd = (command or "").strip().lower()

        if cmd == "view":
            return handler.view(rel)
        if cmd == "create":
            result = handler.create(rel, file_text or "")
            if result.startswith("Created"):
                _maybe_log(
                    scope_id, config, "memory_write",
                    {"command": "create", "path": rel,
                     "bytes": len((file_text or "").encode("utf-8"))},
                )
            return result
        if cmd == "str_replace":
            result = handler.str_replace(rel, old_str or "", new_str or "")
            if result.startswith("Updated"):
                _maybe_log(
                    scope_id, config, "memory_write",
                    {"command": "str_replace", "path": rel},
                )
            return result
        if cmd == "insert":
            result = handler.insert(rel, insert_line or 0, new_str or "")
            if result.startswith("Inserted"):
                _maybe_log(
                    scope_id, config, "memory_write",
                    {"command": "insert", "path": rel, "at_line": insert_line or 0},
                )
            return result
        if cmd == "delete":
            result = handler.delete(rel)
            if result.startswith("Deleted"):
                _maybe_log(
                    scope_id, config, "memory_write",
                    {"command": "delete", "path": rel},
                )
            return result
        if cmd == "rename":
            result = handler.rename(rel_old, rel_new)
            if result.startswith("Renamed"):
                _maybe_log(
                    scope_id, config, "memory_write",
                    {"command": "rename", "from": rel_old, "to": rel_new},
                )
            return result
        return (
            f"Error: unknown command {cmd!r}. Valid: view, create, "
            "str_replace, insert, delete, rename."
        )

    return StructuredTool.from_function(
        coroutine=_run,
        name="memory",
        description=_MEMORY_DESCRIPTION,
        args_schema=MemoryInput,
    )


# ─── system-prompt rider (added when profile.memory.auto_view_at_start) ─


MEMORY_PROMPT_RIDER = (
    "\n\n[Memory tool active.\n"
    "  - BEFORE you answer anything that might benefit from prior notes, "
    "call memory(command='view', path='/memories') to see what's there.\n"
    "  - If you make a non-trivial discovery, write it down via "
    "memory(command='create', path='/memories/<name>.md', file_text='...') "
    "so it survives summarisation and reaches future turns.\n"
    "  - Notes scoped to this thread by default; durable across "
    "context resets.]"
)
