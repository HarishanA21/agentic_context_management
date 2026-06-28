import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Any, AsyncIterator, Dict, List, Optional

import requests

import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from jwt import PyJWKClient
from langchain.agents import create_agent
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres import PostgresSaver


class HybridPostgresSaver(PostgresSaver):
    """PostgresSaver + the async surface that ainvoke needs.

    The stock sync saver inherits the BaseCheckpointSaver async methods,
    which raise NotImplementedError. As soon as we call `agent.ainvoke()`
    (required for async-only MCP tools), LangGraph hits `aget_tuple` /
    `aput` / `aput_writes` and blows up.

    The psycopg ConnectionPool we hand it is thread-safe, so the simplest
    fix is to delegate the async methods to the existing sync ones via
    `asyncio.to_thread`. This lets a single saver back both `invoke` and
    `ainvoke` without standing up a parallel AsyncConnectionPool +
    AsyncPostgresSaver.
    """

    async def aget_tuple(self, config):  # type: ignore[override]
        return await asyncio.to_thread(self.get_tuple, config)

    async def aput(self, config, checkpoint, metadata, new_versions):  # type: ignore[override]
        return await asyncio.to_thread(
            self.put, config, checkpoint, metadata, new_versions
        )

    async def aput_writes(self, config, writes, task_id, task_path=""):  # type: ignore[override]
        return await asyncio.to_thread(
            self.put_writes, config, writes, task_id, task_path
        )

    async def alist(self, config, *, filter=None, before=None, limit=None):  # type: ignore[override]
        # alist is an async generator on the base class. Drain the sync
        # iterator in a worker thread, then yield from the materialized
        # list so we don't hold the worker for the duration of consumption.
        def _drain():
            return list(
                self.list(config, filter=filter, before=before, limit=limit)
            )

        items = await asyncio.to_thread(_drain)
        for item in items:
            yield item
from langgraph.types import Command
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from Tools import all_tools
from agent_callbacks import AgentLogger, EventStreamer
from cancel_registry import clear_cancel, request_cancel
from event_bus import bus as event_bus
from sandbox_client import SandboxError, SandboxNotFoundError, get_backend
from storage import file_key, get_bucket, is_not_found, session_prefix
import github_client

load_dotenv()

DB_URL = os.environ["SUPABASE_DB_URL"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
JWKS_URL = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"
_jwks_client = PyJWKClient(JWKS_URL)

# ── File uploads ────────────────────────────────────────────────────────────
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB per file


def _safe_filename(name: str) -> str:
    """Strip to basename, reject empty / traversal / hidden files."""
    base = Path(name).name  # drops any directory components
    if not base or base in {".", ".."} or base.startswith("."):
        raise HTTPException(400, "Invalid filename")
    return base


def _verify_session(conn, user_id: str, session_id: str):
    if not conn.execute(
        "SELECT 1 FROM sessions WHERE id = %s AND user_id = %s",
        (session_id, user_id),
    ).fetchone():
        raise HTTPException(404, "Session not found")

DEFAULT_MODEL = os.getenv("CHAT_MODEL", "google/gemini-3.1-flash-lite")

# ── Context-management strategy ────────────────────────────────────────────
# Selects how the agent's tool surface is presented to the LLM each turn.
# New strategies plug in as additional branches in `_get_agent_for_request`.
#   tool_calling   — classic ReAct: every tool is its own LangChain tool,
#                    agent calls one per round. Current default.
#   ts_code_mode   — catalog + describe_tools + execute_typescript. The
#                    agent picks tool names from a compact catalog, fetches
#                    full TS interfaces on demand, and runs one TS program
#                    per turn in a Deno isolate. See TS_CODE_MODE_PLAN.md.
_VALID_CONTEXT_STRATEGIES = {"tool_calling", "ts_code_mode"}
DEFAULT_CONTEXT_STRATEGY = (
    os.getenv("DEFAULT_CONTEXT_STRATEGY", "tool_calling").strip() or "tool_calling"
)
if DEFAULT_CONTEXT_STRATEGY not in _VALID_CONTEXT_STRATEGIES:
    print(
        f"[startup] WARNING: DEFAULT_CONTEXT_STRATEGY={DEFAULT_CONTEXT_STRATEGY!r} "
        f"not in {sorted(_VALID_CONTEXT_STRATEGIES)}; falling back to tool_calling.",
        flush=True,
    )
    DEFAULT_CONTEXT_STRATEGY = "tool_calling"


def _normalise_strategy(value: Optional[str]) -> str:
    v = (value or "").strip().lower()
    return v if v in _VALID_CONTEXT_STRATEGIES else DEFAULT_CONTEXT_STRATEGY


# Path to the Deno binary used by `ts_code_mode`. Only resolved when the
# strategy is actually used, so the rest of the backend doesn't care
# whether Deno is installed.
DENO_BIN = os.getenv("DENO_BIN", "deno").strip() or "deno"


def _deno_health_check() -> Optional[str]:
    """Return Deno version string if the binary is callable, else None.
    Run once at startup and logged — does NOT crash the backend, because
    most paths don't need Deno. Only ts_code_mode will fail loudly if
    Deno is missing at request time.
    """
    import subprocess  # local import: only used at startup
    try:
        out = subprocess.run(
            [DENO_BIN, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return (out.stdout or "").splitlines()[0].strip() or "deno (version unknown)"
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _build_model(model_name: str) -> ChatOpenAI:
    # Note: glm-4.5-air:free has a known bug where it wraps multi-arg tool
    # call `args` in a list, breaking AIMessage validation. Llama-3.3 + Qwen-2.5
    # handle structured tool calls correctly.
    # max_tokens deliberately omitted — let each model's upstream provider
    # decide. Reasoning models eat tokens unpredictably and a hard cap clips
    # mid-thought. Set CHAT_MAX_TOKENS env var to bring back the ceiling.
    max_tokens_env = os.getenv("CHAT_MAX_TOKENS", "").strip()
    max_tokens = int(max_tokens_env) if max_tokens_env else None
    return ChatOpenAI(
        model=model_name,
        openai_api_key=os.getenv("OPENROUTER_API_KEY"),
        openai_api_base="https://openrouter.ai/api/v1",
        max_tokens=max_tokens,
        temperature=float(os.getenv("CHAT_TEMPERATURE", "0.3")),
        # Stream tokens so EventStreamer.on_llm_new_token can fan them out
        # over SSE. Without this the agent only sees the full message at
        # end of call and the UI feels like it hangs for the LLM round-trip.
        streaming=True,
        default_headers={
            "HTTP-Referer": "http://localhost",
            "X-Title": "FYP Agent Project",
        },
        # OpenRouter-specific request body: when the primary upstream
        # provider returns 429/5xx, automatically try the next one in the
        # pool instead of bubbling the failure up. Without this, a single
        # throttled provider (e.g. Venice for llama-3.3-70b) blocks every
        # turn even though other providers can serve the same model.
        extra_body={
            "provider": {
                "allow_fallbacks": True,
                "sort": "throughput",
            },
        },
    )


_agent_cache: Dict[str, Any] = {}
_agent_cache_lock = Lock()
# Cap so the cache can't grow unbounded as users × MCP fingerprints multiply.
_AGENT_CACHE_MAX = 64


def _resolve_chat_model(
    model_name_hint: Optional[str],
    user_id: str,
    session_id: Optional[str] = None,
):
    """Pick the LangChain chat model for this user's next /chat turn.

    Order:
      1. The session's `preferred_provider_id` if set (Phase F per-session
         picker).
      2. The user's default provider (`is_default = true`).
      3. Env-var path: `_build_model(model_name_hint)` — legacy OpenRouter
         setup, used when the user has no providers configured.

    Returns `(chat_model, cache_key_str)`. The cache key prefix differs
    between the three branches so they never collide.
    """
    try:
        with app.state.pool.connection() as conn:
            # 1) session-specific override
            if session_id:
                row = conn.execute(
                    """
                    SELECT lp.id, lp.slug, lp.model_id, lp.credentials_blob,
                           lp.updated_at, lp.temperature, lp.max_tokens
                      FROM sessions s
                      JOIN llm_providers lp ON lp.id = s.preferred_provider_id
                     WHERE s.id = %s AND s.user_id = %s
                     LIMIT 1
                    """,
                    (session_id, user_id),
                ).fetchone()
                if row:
                    (
                        pid,
                        slug,
                        model_id,
                        blob,
                        updated_at,
                        temperature,
                        max_tokens,
                    ) = row
                    from providers.base import decrypt_credentials
                    from providers.registry import (
                        _build_for_user,
                        _env_fallback_model,
                    )

                    creds = decrypt_credentials(blob or "")
                    if creds:
                        chat_model = _build_for_user(
                            slug,
                            model_id,
                            creds,
                            updated_at,
                            temperature,
                            max_tokens,
                        )
                        return (
                            chat_model,
                            f"prov::{slug}::{model_id}::{updated_at}::sess",
                        )
                    # creds missing — fall through to user default
                    print(
                        f"[providers] session {session_id} preferred provider "
                        f"{pid} has empty credentials; using user default",
                        flush=True,
                    )

            # 2) user default
            row = conn.execute(
                """
                SELECT slug, model_id, updated_at
                  FROM llm_providers
                 WHERE user_id = %s AND is_default = true
                 LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row:
                from providers.registry import resolve_active_model

                slug, model_id, updated_at = row
                chat_model = resolve_active_model(conn, user_id)
                key = f"prov::{slug}::{model_id}::{updated_at}"
                return chat_model, key
    except Exception as e:
        print(
            f"[providers] resolve_active_model failed for {user_id}; "
            f"falling back to env path: {e!r}",
            flush=True,
        )

    # 3) env fallback
    name = (model_name_hint or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    return _build_model(name), f"env::{name}"


def _unwrap_exc(e: BaseException) -> BaseException:
    """anyio TaskGroups (used by the MCP transports) wrap real errors in
    ExceptionGroup. The outer message is generic; the inner one tells you
    what actually happened. Walk down to the first non-group leaf so the
    chat reply / error log shows the useful cause.
    """
    # ExceptionGroup is stdlib in 3.11+; on older runtimes we just return
    # the original exception unchanged.
    EG = getattr(__builtins__, "ExceptionGroup", None) or globals().get(
        "ExceptionGroup", type(None)
    )
    seen = set()
    current: BaseException = e
    while isinstance(current, EG) and id(current) not in seen:
        seen.add(id(current))
        children = getattr(current, "exceptions", None) or ()
        if not children:
            break
        current = children[0]
    return current


def _get_agent(model_name: Optional[str]):
    """Backwards-compatible helper that builds an agent with only the
    built-in tools. Used at startup (warmup) and any path that hasn't yet
    been threaded through to the MCP-aware variant.
    """
    name = (model_name or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    key = f"{name}::base"
    with _agent_cache_lock:
        if key in _agent_cache:
            return _agent_cache[key]
        agent = create_agent(
            model=_build_model(name),
            tools=all_tools,
            system_prompt=SYSTEM_PROMPT,
            checkpointer=app.state.saver,
        )
        _agent_cache[key] = agent
        return agent


def _fetch_enabled_mcp_rows(user_id: str) -> List[Dict[str, Any]]:
    """Pull the user's enabled MCP servers as plain dicts the adapter can
    consume. Returns [] on DB hiccups — agent still runs with built-ins.
    """
    try:
        with app.state.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT id::text, user_id::text, catalog_slug, is_custom, name,
                       enabled, transport, command, args_json, endpoint_url,
                       auth_kind, auth_header, secret_blob
                  FROM mcp_servers
                 WHERE user_id = %s AND enabled = TRUE
                """,
                (user_id,),
            ).fetchall()
        cols = [
            "id", "user_id", "catalog_slug", "is_custom", "name",
            "enabled", "transport", "command", "args_json", "endpoint_url",
            "auth_kind", "auth_header", "secret_blob",
        ]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        print(f"[mcp] _fetch_enabled_mcp_rows failed: {e!r}", flush=True)
        return []


def _fetch_enabled_skill_rows(user_id: str) -> List[Dict[str, Any]]:
    """Pull the user's enabled skills (catalog + custom) as plain dicts.

    Catalog rows carry only ``catalog_slug`` + ``enabled``; their content is
    resolved from ``skills_catalog.CATALOG`` at prompt-build time. Custom rows
    carry their own name/description/instructions. Returns [] on DB hiccups —
    the agent still runs with no skills folded in.
    """
    try:
        with app.state.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT catalog_slug, is_custom, name, description, instructions
                  FROM skills
                 WHERE user_id = %s AND enabled = TRUE
                 ORDER BY is_custom, created_at
                """,
                (user_id,),
            ).fetchall()
        cols = ["catalog_slug", "is_custom", "name", "description", "instructions"]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        print(f"[skills] _fetch_enabled_skill_rows failed: {e!r}", flush=True)
        return []


def _apply_triggered_skills(
    user_id: str, session_id: str, thread_id: str, names: List[str]
) -> str:
    """Apply skills for this turn — both "/"-forced and auto-matched. For each
    resolved skill we (a) build a prompt block with its full instructions and
    (b) record a `skill_used` display message so the timeline shows
    "Skill: <name>" — persistent, just like a plugin/tool call.

    Returns a directive string (or "" if none resolved) prepended to the turn's
    message. Resolves any skill the user owns (built-in or custom), so a forced
    skill works even when it isn't globally enabled.
    """
    # Custom-skill lookup by name for this user.
    custom: Dict[str, str] = {}
    try:
        with app.state.pool.connection() as conn:
            for nm, instr in conn.execute(
                "SELECT name, instructions FROM skills "
                "WHERE user_id = %s AND is_custom = TRUE",
                (user_id,),
            ).fetchall():
                custom[nm] = instr or ""
    except Exception as e:
        print(f"[skills] custom lookup failed: {e!r}", flush=True)

    from skills_catalog import resolve_skill_instructions_by_name  # lazy

    blocks: List[str] = []
    applied: List[str] = []
    for name in names:
        instructions = resolve_skill_instructions_by_name(name, custom)
        if not instructions:
            continue
        blocks.append(f"## Skill: {name}\n{instructions.strip()}")
        applied.append(name)

    # Record a display message per applied skill so the UI shows it (live via
    # the SSE the recorder emits, and in history on reload).
    if applied:
        try:
            with app.state.pool.connection() as conn:
                for name in applied:
                    _record_message(
                        conn,
                        session_id,
                        thread_id,
                        user_id,
                        "tool",
                        name,
                        tool_name="skill_used",
                    )
        except Exception as e:
            print(f"[skills] record skill_used failed: {e!r}", flush=True)

    if not blocks:
        return ""
    joined = "\n\n".join(blocks)
    return (
        "[The following skill(s) apply to this message and are already loaded — "
        "follow their instructions exactly. Do NOT call read_skill for these; "
        "you already have them.]\n\n" + joined
    )


def _fetch_enabled_plugin_slugs(user_id: str) -> List[str]:
    """Return the slugs of plugins the user has enabled. Each maps to one or
    more real tools added to the agent's toolbox. [] on DB hiccups."""
    try:
        with app.state.pool.connection() as conn:
            rows = conn.execute(
                "SELECT catalog_slug FROM plugins "
                "WHERE user_id = %s AND enabled = TRUE",
                (user_id,),
            ).fetchall()
        return [r[0] for r in rows]
    except Exception as e:
        print(f"[plugins] _fetch_enabled_plugin_slugs failed: {e!r}", flush=True)
        return []


async def _collect_mcp_tools_async(user_id: str):
    """Async half of the agent builder — opens MCP sessions briefly to
    discover tools. Tools are bound to connection specs and re-open
    sessions on each invocation, so the discovery session can close
    safely before we return.
    """
    from mcp_client import collect_tools_for_user

    enabled = _fetch_enabled_mcp_rows(user_id)
    if not enabled:
        return []
    try:
        return await collect_tools_for_user(enabled)
    except Exception as e:
        print(f"[mcp] collect_tools failed for {user_id}: {e!r}", flush=True)
        return []


# ─── vision-capability detection (for the visual method) ────────────────
#
# The visual method rasterises tool output into a PNG the model reads
# instead of text. If the active model can't accept image input, sending
# those blocks hard-fails the turn ("No endpoints found that support image
# input"). We detect support up front and fall back to plain text instead.

# Model-id substrings that are reliably vision-capable across providers.
# Used as a fallback when OpenRouter's metadata isn't available (e.g. a
# native Anthropic/OpenAI/Google provider, not OpenRouter).
_VISION_HINTS = (
    "gpt-4o", "gpt-4.1", "gpt-4-turbo", "gpt-5", "o1", "o3", "o4-mini",
    "gemini", "claude-3", "claude-4", "claude-opus", "claude-sonnet",
    "claude-haiku", "llama-3.2", "llama-4", "pixtral", "qwen-vl",
    "qwen2-vl", "qwen2.5-vl", "qwen3-vl", "llava", "internvl",
    "grok-2-vision", "grok-4", "mistral-small-3", "phi-3-vision",
    "phi-4-multimodal", "molmo", "vision", "vl-",
)

_vision_cache: Dict[str, Any] = {"mods": None, "ts": 0.0}
_vision_lock = Lock()


def _openrouter_modalities() -> Dict[str, set]:
    """Map of OpenRouter ``model_id -> set(input modalities)``, cached ~1h.

    OpenRouter's public /models listing carries
    ``architecture.input_modalities`` (e.g. ``["text", "image"]``) — the
    authoritative answer to "can this model read images". Returns ``{}``
    if the listing can't be fetched, in which case callers fall back to
    the substring heuristic.
    """
    now = time.time()
    with _vision_lock:
        mods = _vision_cache["mods"]
        if mods is not None and (now - _vision_cache["ts"]) < 3600:
            return mods
    out: Dict[str, set] = {}
    try:
        import requests

        r = requests.get("https://openrouter.ai/api/v1/models", timeout=5)
        r.raise_for_status()
        for m in r.json().get("data", []):
            mid = (m.get("id") or "").lower()
            if not mid:
                continue
            arch = m.get("architecture") or {}
            ms = arch.get("input_modalities")
            if not ms:
                # Older shape: "modality": "text+image->text".
                raw = arch.get("modality") or ""
                ms = raw.split("->")[0].split("+") if raw else []
            out[mid] = {str(x).lower() for x in ms}
    except Exception as e:
        print(
            f"[visual_method] OpenRouter modality lookup failed: {e!r} "
            "— using name heuristic",
            flush=True,
        )
    with _vision_lock:
        _vision_cache["mods"] = out
        _vision_cache["ts"] = now
    return out


def _model_id_of(chat_model) -> str:
    """Best-effort model identifier off a LangChain chat model object."""
    for attr in ("model_name", "model", "model_id"):
        v = getattr(chat_model, attr, None)
        if isinstance(v, str) and v:
            return v
    return ""


def _model_supports_vision(model_id: str) -> bool:
    """True if ``model_id`` accepts image input.

    Prefers OpenRouter's metadata (exact, covers the long tail); falls
    back to a substring heuristic for native providers or when the
    listing is unavailable. Unknown models default to *not* vision so the
    visual method degrades to text rather than hard-failing the turn.
    """
    mid = (model_id or "").strip().lower()
    if not mid:
        return False
    mods = _openrouter_modalities()
    if mods:
        for key in (mid, mid.split(":")[0]):  # tolerate ":free" etc. suffixes
            if key in mods:
                return "image" in mods[key]
    return any(h in mid for h in _VISION_HINTS)


def _note_visual_method_skip(
    profile, model_hint, user_id, session_id, thread_id
) -> None:
    """If the profile enables the visual method but the active model can't
    read images, record a single context_event so the UI explains why no
    rasterised tool outputs appear (rather than the feature silently doing
    nothing). Deduped per thread; best-effort, never raises."""
    try:
        vm = getattr(
            getattr(profile, "context_management", None), "visual_method", None
        )
        if not (vm is not None and getattr(vm, "enabled", False)):
            return
        chat_model, _ = _resolve_chat_model(model_hint, user_id, session_id)
        mid = _model_id_of(chat_model)
        if _model_supports_vision(mid):
            return
        with app.state.pool.connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM context_events WHERE user_id=%s AND session_id=%s "
                "AND thread_id=%s AND edit_type=%s LIMIT 1",
                (user_id, session_id, thread_id, "visual_method_skipped"),
            ).fetchone()
            if exists:
                return
            _record_context_event(
                conn,
                user_id=user_id,
                session_id=session_id,
                thread_id=thread_id,
                turn_index=0,
                edit_type="visual_method_skipped",
                details={
                    "model": mid,
                    "note": (
                        f"Visual method is on, but '{mid}' can't read images, "
                        "so tool outputs stayed as text. Switch to a "
                        "vision-capable model (e.g. google/gemini-2.5-flash) "
                        "to enable image compression."
                    ),
                },
            )
    except Exception as e:
        print(f"[visual_method] skip-notice failed: {e!r}", flush=True)


def _get_agent_for_request(
    model_name: Optional[str],
    user_id: str,
    strategy: Optional[str] = None,
    session_id: Optional[str] = None,
    profile: Optional["Profile"] = None,  # type: ignore[name-defined]
):
    """Sync wrapper around the async tool discovery, so /chat (which is
    a sync FastAPI handler) can call us without flipping to async.

    Profile-driven branching (PR #1 onwards):
      * ``profile.tool_surface`` decides the tool list + system prompt
        (``tool_calling`` vs ``ts_code_mode``).
      * Later PRs read ``profile.context_management.{trimming,
        summarisation, memory, subagent, jit_tools, sliding_window}``
        to wire in those techniques. PR #1 leaves them as no-ops.

    Back-compat: callers that haven't migrated still pass a string
    ``strategy``; we wrap it into a synthetic profile. New callers
    pass a fully-resolved Profile via ``profile=``.

    Cache key includes the profile's fingerprint so changing any
    technique toggle rebuilds the agent on the next request.
    """
    # Resolve to a Profile object regardless of how the caller invoked us.
    if profile is None:
        from context_profiles import Profile as _Profile  # lazy import

        # Legacy string path: build a synthetic profile from the strategy
        # name. ``minimal``/``code_mode`` are the only two outcomes here.
        surface = _normalise_strategy(strategy)
        profile = _Profile(tool_surface=surface)
    surface = profile.tool_surface
    profile_fp = profile.fingerprint()

    # Choose the LLM. Order: session.preferred_provider_id (Phase F),
    # then user default, then env fallback.
    chat_model, model_key = _resolve_chat_model(model_name, user_id, session_id)

    mcp_tools = asyncio.run(_collect_mcp_tools_async(user_id))
    # Plugins (user-added capabilities): each enabled plugin contributes one or
    # more real tools to the agent's toolbox. They join MCP + built-in tools and
    # feed the fingerprint below, so enabling/disabling a plugin rebuilds the
    # cached agent automatically.
    from plugins_catalog import build_plugin_tools  # lazy import

    plugin_tools = build_plugin_tools(_fetch_enabled_plugin_slugs(user_id))
    real_tools = list(all_tools) + list(mcp_tools) + list(plugin_tools)

    # JIT tools (technique B5): the find/head/tail/grep retrieval primitives are
    # opt-in. Attach them to the real tool surface only when the profile enables
    # the toggle, so the technique can be A/B-tested. Added to `real_tools`
    # (before the surface branch) so both tool_calling and ts_code_mode — and
    # any sub-agent that inherits real_tools — pick them up.
    jit_cfg = getattr(getattr(profile, "context_management", None), "jit_tools", None)
    if jit_cfg is not None and getattr(jit_cfg, "enabled", False):
        from Tools import jit_retrieval_tools

        real_tools = real_tools + list(jit_retrieval_tools)

    real_fingerprint = ",".join(sorted(t.name for t in real_tools))

    if surface == "ts_code_mode":
        # Local imports to keep the import graph clean: tool_calling
        # turns don't pay the cost of loading ts_code_mode at all.
        from Tools.describe_tools_tool import make_describe_tools_tool
        from Tools.execute_typescript_tool import make_execute_typescript_tool
        from ts_code_mode import ts_code_mode_system_prompt

        agent_tools = [
            make_describe_tools_tool(real_tools),
            make_execute_typescript_tool(real_tools),
        ]
        system_prompt = ts_code_mode_system_prompt(real_tools)
    else:
        agent_tools = real_tools
        system_prompt = SYSTEM_PROMPT

    # PR #4: memory tool. Added to the agent's toolbox when the profile
    # enables it. The tool reads its scope (thread vs user) from this
    # config block at call time — so the agent cache key only depends on
    # `scope`, not on the live thread_id. system_prompt rider nudges the
    # model to view memory at turn start.
    mem_cfg = getattr(getattr(profile, "context_management", None), "memory", None)
    if mem_cfg is not None and getattr(mem_cfg, "enabled", False):
        from Tools.memory_tool import MEMORY_PROMPT_RIDER, make_memory_tool

        agent_tools = list(agent_tools) + [make_memory_tool(scope=mem_cfg.scope)]
        if getattr(mem_cfg, "auto_view_at_start", True):
            system_prompt = system_prompt + MEMORY_PROMPT_RIDER

    # PR #7: sub-agent delegation tool. Bound to the parent's chat_model
    # and tool surface so the subagent stays apples-to-apples. The tool
    # itself stays lightweight; the heavy lifting is in subagent.py.
    sub_cfg = getattr(getattr(profile, "context_management", None), "subagent", None)
    if sub_cfg is not None and getattr(sub_cfg, "enabled", False):
        from Tools.delegate_tool import make_delegate_tool

        agent_tools = list(agent_tools) + [
            make_delegate_tool(
                real_tools=real_tools,
                tool_surface=surface,
                chat_model=chat_model,
                base_system_prompt=SYSTEM_PROMPT,
                token_budget=sub_cfg.token_budget,
                max_depth=sub_cfg.max_depth,
            )
        ]

    # Visual method: rasterise large tool outputs into a formatted image the
    # model reads instead of raw text. Wraps the tool list so every tool's
    # return above the threshold flows through the compressor. Requires a
    # vision-capable chat model. Runs last so it wraps memory/subagent tools too.
    vm_cfg = getattr(getattr(profile, "context_management", None), "visual_method", None)
    if vm_cfg is not None and getattr(vm_cfg, "enabled", False):
        # The visual method emits image content blocks. A model that can't
        # accept image input hard-fails the turn ("No endpoints found that
        # support image input"), so only wrap when the active model is
        # vision-capable — otherwise fall back to plain text.
        model_id = _model_id_of(chat_model)
        if not _model_supports_vision(model_id):
            print(
                f"[visual_method] model {model_id!r} has no image input — "
                "skipping visual compression, using plain text",
                flush=True,
            )
        else:
            try:
                from visual_tool.wrap_tools import wrap_tools_with_compression  # lazy

                agent_tools = wrap_tools_with_compression(
                    agent_tools,
                    mode=vm_cfg.mode,
                    threshold_tokens=vm_cfg.threshold_tokens,
                    only_tools=vm_cfg.only_tools or None,
                    exclude_tools=vm_cfg.exclude_tools or None,
                )
            except Exception as e:
                print(f"[visual_method] wrap failed: {e!r} — using plain tools", flush=True)

    # Skills (progressive disclosure): the system prompt lists enabled skills as
    # JSON (name + description only — no instructions). The model inspects that
    # list and, when a skill matches the task, calls the `read_skill` tool to
    # load its full instructions on demand — which also surfaces in the UI as a
    # visible "Skill: <name>" step. The fingerprint hashes the manifest + the
    # instructions so toggling/editing a skill rebuilds the cached agent.
    # (The "/" menu remains an explicit override — see _apply_triggered_skills.)
    import hashlib
    import json as _json

    from skills_catalog import (  # lazy import
        build_skills_system_prompt,
        skill_instructions_index,
    )

    _skill_rows = _fetch_enabled_skill_rows(user_id)
    skills_rider = build_skills_system_prompt(_skill_rows)
    skill_index = skill_instructions_index(_skill_rows)
    if skills_rider:
        system_prompt = system_prompt + skills_rider
    if skill_index:
        from Tools.use_skill_tool import make_read_skill_tool  # lazy

        agent_tools = list(agent_tools) + [make_read_skill_tool(skill_index)]
    skills_fp = hashlib.sha1(
        (skills_rider + _json.dumps(skill_index, sort_keys=True)).encode()
    ).hexdigest()[:12]

    # Cache key includes the profile fingerprint (covers tool_surface +
    # every technique toggle). Two profiles with identical bodies share
    # an agent; flipping any toggle invalidates without disturbing
    # unrelated users' cached agents.
    profile_hash = hashlib.sha1(profile_fp.encode()).hexdigest()[:12]
    key = f"{model_key}::{user_id}::{profile_hash}::{skills_fp}::{real_fingerprint}"
    with _agent_cache_lock:
        if key in _agent_cache:
            return _agent_cache[key]
        if len(_agent_cache) >= _AGENT_CACHE_MAX:
            _agent_cache.pop(next(iter(_agent_cache)), None)
        # Recover tool calls that Gemini-family models leak as text
        # (``default_api.tool(...)``) so the agent still executes them.
        # Safe no-op for models that return structured tool calls.
        from tool_call_recovery import LeakedToolCallMiddleware

        agent = create_agent(
            model=chat_model,
            tools=agent_tools,
            system_prompt=system_prompt,
            checkpointer=app.state.saver,
            middleware=_image_recall_middleware(profile)
            + [LeakedToolCallMiddleware()],
        )
        _agent_cache[key] = agent
        return agent


def _image_recall_middleware(profile) -> list:
    """Middleware list for the image-recall techniques. Attaches the unified
    ImageRecallMiddleware whenever the profile's mode is not ``off``. The
    middleware applies caching (cache breakpoints) and/or within-loop image
    eviction per the mode. The *persistent* between-turns eviction (with its
    UI event marker) still runs in context_editing.apply_context_edits; the
    middleware's eviction is the per-call view and is idempotent with it.
    """
    ir = getattr(getattr(profile, "context_management", None), "image_recall", None)
    mode = getattr(ir, "mode", "off") if ir is not None else "off"
    if not ir or mode == "off":
        return []
    from cache_layout import ImageRecallMiddleware  # lazy

    return [
        ImageRecallMiddleware(
            mode=mode,
            keep_recent_images=getattr(ir, "keep_recent_images", 3),
            ttl=getattr(ir, "cache_ttl", "5m"),
        )
    ]


# ── OpenRouter model catalog (cached) ───────────────────────────────────────
_models_cache: Dict[str, Any] = {"at": 0.0, "items": []}
_models_lock = Lock()
_MODELS_TTL_SECONDS = 600  # 10 minutes


# Paid OpenRouter models we explicitly promote into the model picker.
# Kept tiny — anything in here gets billed when selected, so only list
# models the user has explicitly opted into. The Visual Compression
# Bench tab of the Strategy Demo recommends gemini-2.5-flash and the
# bench numbers in VISUAL_COMPRESSION_BENCH.md cite it, so it needs to
# be selectable from the dropdown.
_PROMOTED_PAID_MODELS: set[str] = {
    "google/gemini-2.5-flash",   # Visual Compression Bench default
    "inclusionai/ring-2.6-1t",   # Manually promoted — large context model
    # Primary models for chat / demo / projects (requested set).
    "minimax/minimax-m3",
    "stepfun/step-3.7-flash",
    "google/gemini-3.1-flash-lite",
}

# Models pinned manually so they always appear in the picker regardless of
# whether OpenRouter's catalog returns them. Merged after the live fetch;
# any model already returned by the catalog keeps the catalog's metadata.
_PINNED_MODELS: List[Dict[str, Any]] = [
    {
        "id": "inclusionai/ring-2.6-1t",
        "name": "Ring 2.6 1T",
        "context_length": 32768,
        "description": "Ring 2.6 1T — via OpenRouter.",
        "vision": False,
    },
    # Primary models for chat / demo / projects. Pinned so they always show
    # in the picker even if OpenRouter's live catalog hasn't surfaced them
    # yet; the catalog's metadata (incl. real context_length) wins when it
    # does return them. context_length here is a best-effort fallback.
    {
        "id": "google/gemini-3.1-flash-lite",
        "name": "Gemini 3.1 Flash Lite",
        "context_length": 1048576,
        "description": "Google Gemini 3.1 Flash Lite — fast, cheap, large context. Default chat/demo model.",
        "vision": True,
    },
    {
        "id": "google/gemini-2.5-flash",
        "name": "Gemini 2.5 Flash",
        "context_length": 1048576,
        "description": "Google Gemini 2.5 Flash — vision-capable; required for the Visual method.",
        "vision": True,
    },
    {
        "id": "minimax/minimax-m3",
        "name": "MiniMax M3",
        "context_length": 1000000,
        "description": "MiniMax M3 — large-context reasoning model via OpenRouter.",
        "vision": False,
    },
    {
        "id": "stepfun/step-3.7-flash",
        "name": "StepFun Step 3.7 Flash",
        "context_length": 128000,
        "description": "StepFun Step 3.7 Flash — fast general-purpose model via OpenRouter.",
        "vision": False,
    },
]


def _fetch_free_models() -> List[Dict[str, Any]]:
    """Pull the OpenRouter catalog and keep `:free` models + a small
    allow-list of paid models we want promoted (see
    ``_PROMOTED_PAID_MODELS``). Cached for ``_MODELS_TTL_SECONDS``."""
    now = time.time()
    with _models_lock:
        if _models_cache["items"] and (now - _models_cache["at"]) < _MODELS_TTL_SECONDS:
            return _models_cache["items"]
    data: List[Dict[str, Any]] = []
    try:
        r = requests.get("https://openrouter.ai/api/v1/models", timeout=10)
        r.raise_for_status()
        data = r.json().get("data", [])
    except Exception as e:
        # Don't bail to a bare cache — we still want the pinned primary
        # models to show even when OpenRouter is unreachable. Fall through
        # with no catalog data and let the pinned-merge below guarantee the
        # primary models are present.
        print(f"[/models] OpenRouter fetch failed: {e!r}", flush=True)
        data = []
    items = []
    seen_ids: set[str] = set()
    for m in data:
        mid = m.get("id") or ""
        if not mid.endswith(":free") and mid not in _PROMOTED_PAID_MODELS:
            continue
        # Vision capability straight from the catalog's architecture block,
        # so the picker can flag which models accept images (the Visual
        # method needs one). Falls back to the name heuristic if absent.
        arch = m.get("architecture") or {}
        mods = arch.get("input_modalities")
        if not mods:
            raw = arch.get("modality") or ""
            mods = raw.split("->")[0].split("+") if raw else []
        vision = "image" in {str(x).lower() for x in mods} or _model_supports_vision(mid)
        items.append({
            "id": mid,
            "name": m.get("name") or mid,
            "context_length": m.get("context_length") or 0,
            "description": (m.get("description") or "")[:200],
            "vision": vision,
        })
        seen_ids.add(mid)
    # Merge pinned models that the catalog didn't return — guarantees the
    # primary models are always selectable, online or not.
    for pinned in _PINNED_MODELS:
        if pinned["id"] not in seen_ids:
            entry = dict(pinned)
            entry.setdefault("vision", _model_supports_vision(entry["id"]))
            items.append(entry)
            seen_ids.add(entry["id"])
    items.sort(key=lambda m: m["name"].lower())
    with _models_lock:
        _models_cache["items"] = items
        _models_cache["at"] = now
    return items


class CreateSessionRequest(BaseModel):
    name: str
    kind: Optional[str] = "chat"  # "project" auto-creates starter files
    # GitHub linkage — only meaningful when kind="project".
    #   "none"          — no GitHub link (default)
    #   "new_repo"      — create a fresh repo on the user's account
    #   "link_existing" — point this project at an existing repo
    github_mode: Optional[str] = "none"
    github_repo_name: Optional[str] = None   # for new_repo
    github_private: Optional[bool] = True    # for new_repo
    github_owner: Optional[str] = None       # for link_existing
    github_repo: Optional[str] = None        # for link_existing
    github_branch: Optional[str] = None      # for link_existing (default main)


class CreateThreadRequest(BaseModel):
    name: str


class ChatRequest(BaseModel):
    session_id: str
    thread_id: str
    message: str
    attached_files: List[str] = []
    model: Optional[str] = None
    # Legacy: "tool_calling" / "ts_code_mode" — maps to a built-in
    # preset of the matching tool_surface. Superseded by context_profile_id
    # / context_profile but kept so old clients keep working.
    context_strategy: Optional[str] = None
    # Preferred (PR #1+): a saved profile id, or a one-off profile body.
    # Resolution order: body > id > legacy strategy > session profile
    # > user default > built-in `minimal`. See context_profiles.resolve_profile.
    context_profile_id: Optional[str] = None
    context_profile: Optional[Dict[str, Any]] = None
    # Skills the user force-activated for THIS message via the "/" menu. These
    # are applied for the turn regardless of whether they're globally enabled.
    triggered_skills: List[str] = []


class TitleRequest(BaseModel):
    text: str


class GithubTokenRequest(BaseModel):
    token: str


class CreateWorkspaceRequest(BaseModel):
    session_id: str


class CreateGithubRepoRequest(BaseModel):
    name: str
    private: bool = True


# PostgresSaver now shares the main ConnectionPool — see lifespan() — so
# this used to be a context-manager handle and is no longer needed. Kept
# as a marker in case any leftover reference imports it.
_saver_cm = None

SYSTEM_PROMPT = (
    "You are a helpful assistant with access to project files via tools.\n"
    "\n"
    "CRITICAL RULES — follow strictly:\n"
    "1. When the user asks you to CREATE, WRITE, MODIFY, or SAVE anything, "
    "you MUST call the write_project_file tool in the SAME response. "
    "Do not say 'I will write...' or 'Let me write...' — actually call the tool now.\n"
    "2. When the user asks about a file by name, ALWAYS call read_project_file "
    "before answering. Do not say you can't read the file before trying.\n"
    "3. When the user asks what files exist or refers to 'my files', call "
    "list_project_files first.\n"
    "4. Never claim you wrote a file unless write_project_file just returned "
    "a success message (it starts with 'Wrote'). If it returned an Error, "
    "tell the user what went wrong.\n"
    "\n"
    "PROJECT BOOKKEEPING — applies only when architecture.md and report.md "
    "exist in the project (you can confirm with list_project_files):\n"
    "5. AFTER you write or modify any project file (other than architecture.md "
    "and report.md themselves), update BOTH:\n"
    "   a) architecture.md — read it, then write it back with the structure "
    "section updated to reflect the new/changed file. Keep the existing "
    "format and headings.\n"
    "   b) report.md — read it, then write it back with ONE new line appended "
    "at the very end, formatted exactly:\n"
    "      - <date>: <one-line summary of the change>\n"
    "      Use today's date in YYYY-MM-DD if you know it; otherwise write "
    "'today'. Keep summaries to a single short sentence.\n"
    "6. DO NOT recurse: do NOT update architecture.md or report.md in response "
    "to changes to architecture.md or report.md themselves.\n"
    "7. If multiple files changed in the same turn, do ONE combined update to "
    "architecture.md and ONE combined log entry in report.md — not one per file.\n"
    "\n"
    "Remember everything the user tells you across this project/session."
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One pool for everything — sessions/messages queries AND the LangGraph
    # checkpoint saver. Pool-backed saver self-heals when Postgres closes
    # idle connections, which used to surface as
    # `Checkpoint state error: the connection is closed` after a long idle
    # period or a docker-compose restart.
    pool = ConnectionPool(
        DB_URL, min_size=1, max_size=10, kwargs={"autocommit": True}
    )
    pool.wait()

    saver = HybridPostgresSaver(pool)
    saver.setup()

    # Idempotent migrations for fields added after db/init.sql first shipped.
    with pool.connection() as conn:
        conn.execute(
            "ALTER TABLE messages "
            "ADD COLUMN IF NOT EXISTS input_tokens    integer NOT NULL DEFAULT 0, "
            "ADD COLUMN IF NOT EXISTS output_tokens   integer NOT NULL DEFAULT 0, "
            "ADD COLUMN IF NOT EXISTS thinking_tokens integer NOT NULL DEFAULT 0, "
            "ADD COLUMN IF NOT EXISTS cache_read_tokens integer NOT NULL DEFAULT 0, "
            "ADD COLUMN IF NOT EXISTS langgraph_id    text"
        )
        conn.execute(
            "ALTER TABLE sessions "
            "ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'chat'"
        )
        conn.execute(
            "ALTER TABLE sessions "
            "ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'auto'"
        )
        # Phase F — per-user multi-provider configs and per-session
        # override. Matches db/init.sql; gated by IF NOT EXISTS so it's
        # a no-op when the DB was bootstrapped fresh from the SQL file.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_providers (
                id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id            uuid NOT NULL,
                slug               text NOT NULL,
                label              text NOT NULL,
                model_id           text NOT NULL,
                credentials_blob   text NOT NULL,
                is_default         boolean NOT NULL DEFAULT false,
                last_error         text,
                last_tested_at     timestamptz,
                created_at         timestamptz NOT NULL DEFAULT now(),
                updated_at         timestamptz NOT NULL DEFAULT now(),
                UNIQUE (user_id, label)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_providers_user "
            "ON llm_providers(user_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_providers_user_default "
            "ON llm_providers(user_id) WHERE is_default"
        )
        # Phase G — provider-level temperature + max_tokens overrides.
        conn.execute(
            "ALTER TABLE llm_providers "
            "ADD COLUMN IF NOT EXISTS temperature double precision, "
            "ADD COLUMN IF NOT EXISTS max_tokens integer"
        )
        # Per-session preferred provider; FK with ON DELETE SET NULL so
        # deleting a provider silently reverts affected sessions to the
        # user-level default.
        conn.execute(
            "ALTER TABLE sessions "
            "ADD COLUMN IF NOT EXISTS preferred_provider_id uuid "
            "REFERENCES llm_providers(id) ON DELETE SET NULL"
        )
        # Context-management observability: append-only log of every
        # context edit a strategy applies (trimming, summarisation,
        # sliding-window, sub-agent dispatch, …). Plain bigserial id
        # so the API can ORDER BY id ASC and the UI gets stable order
        # even when several edits land in the same millisecond.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS context_events (
                id            bigserial PRIMARY KEY,
                user_id       uuid NOT NULL,
                session_id    uuid NOT NULL,
                thread_id     uuid NOT NULL,
                turn_index    integer NOT NULL,
                edit_type     text NOT NULL,
                freed_tokens  integer NOT NULL DEFAULT 0,
                details_json  jsonb,
                created_at    timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_context_events_thread "
            "ON context_events(session_id, thread_id, id)"
        )
        # Context-management profiles — bundles tool_surface + per-
        # technique toggles. Built-in presets have user_id IS NULL;
        # user-saved profiles carry the owner's id. See
        # backend/context_profiles.py for the schema + seed routine.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS context_profiles (
                id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id      uuid,
                name         text NOT NULL,
                body         jsonb NOT NULL,
                is_default   boolean NOT NULL DEFAULT false,
                created_at   timestamptz NOT NULL DEFAULT now(),
                updated_at   timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        # Partial unique indexes — separate namespaces for built-ins
        # (user_id NULL) and user-owned. Postgres treats each NULL
        # as distinct so a plain UNIQUE (user_id, name) won't enforce
        # built-in name uniqueness.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uniq_context_profiles_global_name "
            "ON context_profiles(name) WHERE user_id IS NULL"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uniq_context_profiles_user_name "
            "ON context_profiles(user_id, name) WHERE user_id IS NOT NULL"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_context_profiles_user "
            "ON context_profiles(user_id)"
        )
        conn.execute(
            "ALTER TABLE sessions "
            "ADD COLUMN IF NOT EXISTS context_profile_id uuid "
            "REFERENCES context_profiles(id) ON DELETE SET NULL"
        )

        # Skills — toggleable instruction bundles folded into the agent's
        # system prompt (claude.ai-style). Catalog skills carry only a slug +
        # enabled flag (content lives in skills_catalog.py); custom skills
        # carry their own name/description/instructions. Matches db/init.sql.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS skills (
                id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id       uuid NOT NULL,
                catalog_slug  text,
                is_custom     boolean NOT NULL DEFAULT false,
                name          text NOT NULL,
                description   text NOT NULL DEFAULT '',
                instructions  text NOT NULL DEFAULT '',
                enabled       boolean NOT NULL DEFAULT false,
                created_at    timestamptz NOT NULL DEFAULT now(),
                updated_at    timestamptz NOT NULL DEFAULT now(),
                UNIQUE (user_id, catalog_slug)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_skills_user ON skills(user_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_skills_user_enabled "
            "ON skills(user_id) WHERE enabled"
        )

        # Plugins — per-user enabled state for code-defined plugins. Each
        # enabled plugin adds real tools to the agent (see plugins_catalog.py).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS plugins (
                id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id       uuid NOT NULL,
                catalog_slug  text NOT NULL,
                enabled       boolean NOT NULL DEFAULT false,
                created_at    timestamptz NOT NULL DEFAULT now(),
                updated_at    timestamptz NOT NULL DEFAULT now(),
                UNIQUE (user_id, catalog_slug)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_plugins_user_enabled "
            "ON plugins(user_id) WHERE enabled"
        )

        # Seed the built-in context-management presets (minimal,
        # code_mode, long_chat, power_research, cheap_long). Idempotent:
        # the seeder updates existing built-in rows so adding a field
        # to a preset auto-deploys.
        from context_profiles import seed_builtin_presets  # lazy import
        seed_builtin_presets(conn)

    app.state.saver = saver
    app.state.pool = pool

    # Pre-build the agent for the default model so the first /chat request
    # doesn't pay the create_agent cost. Other models are built lazily by
    # _get_agent on first use.
    _get_agent(DEFAULT_MODEL)

    # Deno availability is informational at startup — only ts_code_mode
    # turns will actually need it, and they fail loudly with a clear
    # message if it's missing. Most setups don't use ts_code_mode.
    deno_ver = _deno_health_check()
    if deno_ver:
        print(f"[startup] Deno OK ({deno_ver}) — ts_code_mode available.", flush=True)
    else:
        print(
            f"[startup] Deno not found at {DENO_BIN!r}. "
            f"ts_code_mode turns will error. Install Deno (`brew install deno`) "
            f"to enable, or ignore this if you're staying on tool_calling.",
            flush=True,
        )

    # Workspace garbage collector — destroys expired, pauses idle.
    gc_task = asyncio.create_task(_workspace_gc_loop(app))

    try:
        yield
    finally:
        gc_task.cancel()
        try:
            await gc_task
        except asyncio.CancelledError:
            pass
        _agent_cache.clear()
        # PostgresSaver doesn't own its connections anymore — the pool does.
        pool.close()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# MCP inventory routes (Slice A — catalog + read-only servers listing).
# Import here, after `app` is defined and after `get_current_user` exists,
# to avoid circular imports inside the router.
from routes_mcp import router as mcp_router  # noqa: E402
app.include_router(mcp_router)

from routes_demo import router as demo_router  # noqa: E402
app.include_router(demo_router)

from routes_providers import router as providers_router  # noqa: E402
app.include_router(providers_router)

from routes_context_profiles import router as context_profiles_router  # noqa: E402
app.include_router(context_profiles_router)

from routes_skills import router as skills_router  # noqa: E402
app.include_router(skills_router)

from routes_plugins import router as plugins_router  # noqa: E402
app.include_router(plugins_router)


def get_current_user(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256", "RS256"],
            audience="authenticated",
        )
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"Invalid token: {e}")
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(401, "Token missing subject")
    return sub


def _verify_jwt(token: str) -> str:
    """Verify a Supabase JWT and return the subject (user_id).

    Mirrors `get_current_user` but accepts the raw token directly — used by
    the SSE endpoint, which gets the token via query string because
    EventSource cannot send Authorization headers.
    """
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256", "RS256"],
            audience="authenticated",
        )
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"Invalid token: {e}")
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(401, "Token missing subject")
    return sub


def _verify_thread(conn, user_id: str, session_id: str, thread_id: str):
    row = conn.execute(
        "SELECT 1 FROM threads WHERE id = %s AND session_id = %s AND user_id = %s",
        (thread_id, session_id, user_id),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Thread not found")


def _tool_content_to_text(content) -> str:
    """Flatten a (possibly multimodal) tool result to text for the messages
    table. When the visual method is on, a tool result is a list of blocks
    (text REFERENCES + a base64 image); we keep the text and replace the image
    with an ``[image]`` marker so base64 never bloats the DB / transcript. The
    real image stays in the LangGraph checkpoint, which is what the model reads."""
    if isinstance(content, str):
        return content
    try:
        from context_editing import _flatten_content_for_text  # lazy

        return _flatten_content_for_text(content)
    except Exception:
        return str(content)


def _record_message(
    conn,
    session_id: str,
    thread_id: str,
    user_id: str,
    role: str,
    content: str,
    tool_name: Optional[str] = None,
    tool_calls: Optional[list] = None,
    tokens: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    thinking_tokens: int = 0,
    cache_read_tokens: int = 0,
    langgraph_id: Optional[str] = None,
) -> int:
    row = conn.execute(
        """
        INSERT INTO messages
            (session_id, thread_id, user_id, role, content, tool_name,
             tool_calls_json, tokens, input_tokens, output_tokens,
             thinking_tokens, cache_read_tokens, langgraph_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            session_id,
            thread_id,
            user_id,
            role,
            content,
            tool_name,
            json.dumps(tool_calls) if tool_calls else None,
            int(tokens or 0),
            int(input_tokens or 0),
            int(output_tokens or 0),
            int(thinking_tokens or 0),
            int(cache_read_tokens or 0),
            langgraph_id,
        ),
    ).fetchone()
    new_id = int(row[0]) if row else 0
    # Push to live UI subscribers. Best-effort — never block the DB write
    # path even if every subscriber's queue is full.
    try:
        event_bus.publish(
            f"thread:{thread_id}",
            {
                "type": "message",
                "id": new_id,
                "role": role,
                "content": content,
                "tool_name": tool_name,
                "tool_calls": tool_calls,
            },
        )
    except Exception as e:
        print(f"[event_bus] publish message failed: {e!r}", flush=True)
    return new_id


def _ai_token_breakdown(msg) -> Dict[str, int]:
    """Best-effort token breakdown for an AIMessage. Returns:
      total    — input + output for this turn
      input    — prompt tokens sent to the model
      output   — total completion tokens (visible answer + reasoning)
      thinking — reasoning portion of output (subset of `output`)

    Invariants the UI relies on: input + output = total, and thinking <= output.
    Most non-reasoning models report thinking = 0."""
    total = 0
    input_ = 0
    output = 0
    thinking = 0

    um = getattr(msg, "usage_metadata", None)
    if um:
        def _g(obj, key: str) -> int:
            try:
                if hasattr(obj, "__getitem__"):
                    return int(obj.get(key, 0) or 0)
                return int(getattr(obj, key, 0) or 0)
            except Exception:
                return 0
        total = _g(um, "total_tokens")
        input_ = _g(um, "input_tokens")
        output = _g(um, "output_tokens")
        details = None
        try:
            details = um["output_token_details"] if hasattr(um, "__getitem__") else getattr(um, "output_token_details", None)
        except Exception:
            details = None
        if details:
            thinking = _g(details, "reasoning")

    if not total:
        rm = getattr(msg, "response_metadata", None) or {}
        tu = rm.get("token_usage") or rm.get("usage") or {}
        try:
            total = int(tu.get("total_tokens", 0) or 0)
            input_ = input_ or int(tu.get("prompt_tokens", 0) or 0)
            output = output or int(tu.get("completion_tokens", 0) or 0)
            details = tu.get("completion_tokens_details") or {}
            thinking = thinking or int(details.get("reasoning_tokens", 0) or 0)
        except Exception:
            pass

    # Self-heal in case the provider only sent two of the three.
    if total and not input_ and output:
        input_ = max(0, total - output)
    if total and not output and input_:
        output = max(0, total - input_)
    if not total and (input_ or output):
        total = input_ + output

    # Image-recall caching: how much of `input` was served from the provider
    # prompt cache (cache_read) vs written into it (cache_write). Zero when
    # caching is off or the provider doesn't report it.
    try:
        from cache_layout import read_cache_tokens  # lazy

        ct = read_cache_tokens(msg)
    except Exception:
        ct = {"cache_read": 0, "cache_write": 0}

    return {
        "total": total,
        "input": input_,
        "output": output,
        "thinking": thinking,
        "cache_read": ct["cache_read"],
        "cache_write": ct["cache_write"],
    }


def _record_error_reply(
    session_id: str, thread_id: str, user_id: str, error_msg: str
) -> None:
    """Persist an assistant-side error message so the user sees what went wrong
    on refresh, instead of an unexplained gap after their message."""
    short = error_msg[:300]
    try:
        with app.state.pool.connection() as conn:
            _record_message(
                conn,
                session_id,
                thread_id,
                user_id,
                "assistant",
                f"Error: {short}",
            )
    except Exception as e:
        print(f"[/chat] could not record error reply: {e!r}", flush=True)


@app.get("/sessions")
def list_sessions(user_id: str = Depends(get_current_user)):
    with app.state.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.name, s.kind, s.mode, s.created_at,
                   s.github_owner, s.github_repo, s.github_branch,
                   s.preferred_provider_id,
                   COALESCE(SUM(m.tokens), 0)::int AS tokens
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            WHERE s.user_id = %s
            GROUP BY s.id, s.name, s.kind, s.mode, s.created_at,
                     s.github_owner, s.github_repo, s.github_branch,
                     s.preferred_provider_id
            ORDER BY s.created_at DESC
            """,
            (user_id,),
        ).fetchall()
    return [
        {
            "id": str(r[0]),
            "name": r[1],
            "kind": r[2] or "chat",
            "mode": r[3] or "auto",
            "created_at": r[4].isoformat(),
            "github_owner": r[5],
            "github_repo": r[6],
            "github_branch": r[7],
            "preferred_provider_id": str(r[8]) if r[8] else None,
            "tokens": int(r[9] or 0),
        }
        for r in rows
    ]


class UpdateSessionRequest(BaseModel):
    mode: Optional[str] = None                     # 'auto' | 'confirm'
    # Phase F: per-session provider override.
    #   "<uuid>" — use this specific provider for chats in this session
    #   ""       — clear the override (falls back to user default)
    #   None     — leave unchanged
    preferred_provider_id: Optional[str] = None
    # PR #1: per-session context-management profile override. Same
    # tri-state semantics as preferred_provider_id.
    context_profile_id: Optional[str] = None


@app.patch("/sessions/{session_id}")
def update_session(
    session_id: str,
    req: UpdateSessionRequest,
    user_id: str = Depends(get_current_user),
):
    """Partial-update a session. Supports `mode`, `preferred_provider_id`,
    and `context_profile_id`."""
    if req.mode is not None and req.mode not in {"auto", "confirm"}:
        raise HTTPException(400, "mode must be 'auto' or 'confirm'")

    with app.state.pool.connection() as conn:
        _verify_session(conn, user_id, session_id)
        if req.mode is not None:
            conn.execute(
                "UPDATE sessions SET mode = %s WHERE id = %s AND user_id = %s",
                (req.mode, session_id, user_id),
            )
        if req.preferred_provider_id is not None:
            new_pid: Optional[str]
            if req.preferred_provider_id == "":
                new_pid = None  # clear override
            else:
                # Verify the provider belongs to this user before assigning.
                owned = conn.execute(
                    "SELECT 1 FROM llm_providers "
                    "WHERE id = %s AND user_id = %s",
                    (req.preferred_provider_id, user_id),
                ).fetchone()
                if not owned:
                    raise HTTPException(404, "Provider not found.")
                new_pid = req.preferred_provider_id
            conn.execute(
                "UPDATE sessions SET preferred_provider_id = %s "
                "WHERE id = %s AND user_id = %s",
                (new_pid, session_id, user_id),
            )
        if req.context_profile_id is not None:
            new_cpid: Optional[str]
            if req.context_profile_id == "":
                new_cpid = None  # clear override
            else:
                # Profile must be a built-in (user_id NULL) or owned.
                owned = conn.execute(
                    "SELECT 1 FROM context_profiles "
                    "WHERE id = %s AND (user_id = %s OR user_id IS NULL)",
                    (req.context_profile_id, user_id),
                ).fetchone()
                if not owned:
                    raise HTTPException(404, "Context profile not found.")
                new_cpid = req.context_profile_id
            conn.execute(
                "UPDATE sessions SET context_profile_id = %s "
                "WHERE id = %s AND user_id = %s",
                (new_cpid, session_id, user_id),
            )
    return {
        "ok": True,
        "mode": req.mode,
        "preferred_provider_id": req.preferred_provider_id,
        "context_profile_id": req.context_profile_id,
    }


def _resolve_github_link(
    req: CreateSessionRequest, user_id: str
) -> Optional[dict]:
    """Resolve the session's GitHub linkage based on req.github_mode.

    Performs all GitHub-side work (verify token, create or validate repo)
    *before* the session row is inserted, so we fail fast and never end up
    with an orphan session. Returns `{owner, repo, branch}` to persist, or
    None when no link is requested.
    """
    mode = (req.github_mode or "none").lower()
    if mode not in {"none", "new_repo", "link_existing"}:
        raise HTTPException(400, f"Invalid github_mode: {req.github_mode!r}")
    if mode == "none":
        return None

    with app.state.pool.connection() as conn:
        token = github_client.get_token(conn, user_id)
    if not token:
        raise HTTPException(
            400,
            "Connect a GitHub PAT before linking a project to GitHub.",
        )

    if mode == "new_repo":
        name = (req.github_repo_name or req.name or "").strip()
        if not name:
            raise HTTPException(400, "github_repo_name required for new_repo")
        # Slugify the project name as a fallback (spaces → hyphens, etc.).
        # We accept user-supplied names verbatim and rely on github_client's
        # validation regex to reject anything illegal.
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "project"

        try:
            scope_info = github_client.verify_token_scopes(token)
        except ValueError as e:
            raise HTTPException(401, str(e))
        allowed, reason = github_client.can_create_repos(scope_info)
        if not allowed:
            raise HTTPException(403, reason)
        try:
            info = github_client.create_repo(
                token, name, private=bool(req.github_private)
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {
            "owner": info["owner"],
            "repo": info["repo"],
            "branch": info["default_branch"],
        }

    # link_existing
    owner = (req.github_owner or "").strip()
    repo_name = (req.github_repo or "").strip()
    branch = (req.github_branch or "").strip() or "main"
    if not owner or not repo_name:
        raise HTTPException(
            400, "github_owner and github_repo required for link_existing"
        )
    try:
        gh = github_client.get_client(token)
        gh.get_repo(f"{owner}/{repo_name}")
    except Exception as e:
        raise HTTPException(
            404, f"Could not access {owner}/{repo_name}: {e}"
        )
    return {"owner": owner, "repo": repo_name, "branch": branch}


@app.post("/sessions")
def create_session(req: CreateSessionRequest, user_id: str = Depends(get_current_user)):
    kind = (req.kind or "chat").lower()
    if kind not in {"chat", "project"}:
        kind = "chat"

    github_link: Optional[dict] = None
    if (req.github_mode or "none").lower() != "none":
        if kind != "project":
            raise HTTPException(
                400, "GitHub linkage is only supported on project sessions."
            )
        github_link = _resolve_github_link(req, user_id)

    with app.state.pool.connection() as conn:
        s = conn.execute(
            """
            INSERT INTO sessions
                (user_id, name, kind, github_owner, github_repo, github_branch)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, name, created_at
            """,
            (
                user_id,
                req.name,
                kind,
                github_link["owner"] if github_link else None,
                github_link["repo"] if github_link else None,
                github_link["branch"] if github_link else None,
            ),
        ).fetchone()
        sid, sname, screated = s
        t = conn.execute(
            "INSERT INTO threads (session_id, user_id, name) VALUES (%s, %s, %s) "
            "RETURNING id, name, created_at",
            (sid, user_id, "General"),
        ).fetchone()

    # For projects, seed two starter files the agent maintains over time.
    if kind == "project":
        _seed_project_files(user_id, str(sid), sname)

    return {
        "id": str(sid),
        "name": sname,
        "kind": kind,
        "created_at": screated.isoformat(),
        "tokens": 0,
        "github_owner": github_link["owner"] if github_link else None,
        "github_repo": github_link["repo"] if github_link else None,
        "github_branch": github_link["branch"] if github_link else None,
        "default_thread": {
            "id": str(t[0]),
            "session_id": str(sid),
            "name": t[1],
            "created_at": t[2].isoformat(),
            "tokens": 0,
        },
    }


def _seed_project_files(user_id: str, session_id: str, project_name: str) -> None:
    """Write architecture.md + report.md into the bucket for a new project.
    Best-effort: a failure here shouldn't block session creation."""
    from datetime import datetime

    today = datetime.utcnow().strftime("%Y-%m-%d")
    architecture = (
        f"# {project_name} — Architecture\n"
        "\n"
        "_The agent maintains this document as the project evolves._\n"
        "\n"
        "## Overview\n"
        "\n"
        "_What this project does (one paragraph)._\n"
        "\n"
        "## Components\n"
        "\n"
        "_Major files / modules and their responsibilities._\n"
        "\n"
        "## Data flow\n"
        "\n"
        "_How information moves between components._\n"
    )
    report = (
        f"# {project_name} — Activity log\n"
        "\n"
        f"## {today}\n"
        "- Project created.\n"
    )
    bucket = get_bucket()
    for name, body in (("architecture.md", architecture), ("report.md", report)):
        try:
            bucket.upload(
                path=file_key(user_id, session_id, name),
                file=body.encode("utf-8"),
                file_options={
                    "content-type": "text/markdown; charset=utf-8",
                    "upsert": "true",
                },
            )
        except Exception as e:
            print(f"[create_session] could not seed {name}: {e!r}", flush=True)


@app.get("/sessions/{session_id}/threads")
def list_threads(session_id: str, user_id: str = Depends(get_current_user)):
    with app.state.pool.connection() as conn:
        if not conn.execute(
            "SELECT 1 FROM sessions WHERE id = %s AND user_id = %s",
            (session_id, user_id),
        ).fetchone():
            raise HTTPException(404, "Session not found")
        rows = conn.execute(
            """
            SELECT t.id, t.name, t.created_at,
                   COALESCE(SUM(m.tokens), 0)::int AS tokens
            FROM threads t
            LEFT JOIN messages m ON m.thread_id = t.id
            WHERE t.session_id = %s AND t.user_id = %s
            GROUP BY t.id, t.name, t.created_at
            ORDER BY t.created_at ASC
            """,
            (session_id, user_id),
        ).fetchall()
    return [
        {
            "id": str(r[0]),
            "session_id": session_id,
            "name": r[1],
            "created_at": r[2].isoformat(),
            "tokens": int(r[3] or 0),
        }
        for r in rows
    ]


@app.post("/sessions/{session_id}/threads")
def create_thread(
    session_id: str,
    req: CreateThreadRequest,
    user_id: str = Depends(get_current_user),
):
    with app.state.pool.connection() as conn:
        if not conn.execute(
            "SELECT 1 FROM sessions WHERE id = %s AND user_id = %s",
            (session_id, user_id),
        ).fetchone():
            raise HTTPException(404, "Session not found")
        t = conn.execute(
            "INSERT INTO threads (session_id, user_id, name) VALUES (%s, %s, %s) "
            "RETURNING id, name, created_at",
            (session_id, user_id, req.name),
        ).fetchone()
    return {
        "id": str(t[0]),
        "session_id": session_id,
        "name": t[1],
        "created_at": t[2].isoformat(),
        "tokens": 0,
    }


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str, user_id: str = Depends(get_current_user)):
    with app.state.pool.connection() as conn:
        if not conn.execute(
            "SELECT 1 FROM sessions WHERE id = %s AND user_id = %s",
            (session_id, user_id),
        ).fetchone():
            raise HTTPException(404, "Session not found")
        # threads + messages cascade via FK ON DELETE CASCADE.
        conn.execute(
            "DELETE FROM sessions WHERE id = %s AND user_id = %s",
            (session_id, user_id),
        )
    # Note: LangGraph checkpoint rows for this thread_id still exist in their
    # own tables; they're orphaned but harmless and not user-visible.
    # Uploaded files for this session are intentionally left on disk —
    # an explicit cleanup pass can be added later if storage becomes an issue.
    return {"ok": True}


@app.delete("/sessions/{session_id}/threads/{thread_id}")
def delete_thread(
    session_id: str,
    thread_id: str,
    user_id: str = Depends(get_current_user),
):
    with app.state.pool.connection() as conn:
        _verify_thread(conn, user_id, session_id, thread_id)
        # Messages cascade via FK ON DELETE CASCADE.
        conn.execute(
            "DELETE FROM threads WHERE id = %s AND session_id = %s AND user_id = %s",
            (thread_id, session_id, user_id),
        )
    return {"ok": True}


# ── Sandboxed workspaces ────────────────────────────────────────────────────

WORKSPACE_TTL_HOURS = int(os.environ.get("WORKSPACE_TTL_HOURS", "24"))
WORKSPACE_IDLE_PAUSE_MIN = int(os.environ.get("WORKSPACE_IDLE_PAUSE_MIN", "15"))
WORKSPACE_MAX_PER_USER = int(os.environ.get("WORKSPACE_MAX_PER_USER", "3"))
WORKSPACE_GC_INTERVAL_SEC = int(os.environ.get("WORKSPACE_GC_INTERVAL_SEC", "300"))


def _count_active_workspaces(conn, user_id: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM workspaces "
        "WHERE user_id = %s AND status IN ('running', 'paused')",
        (user_id,),
    ).fetchone()
    return int(row[0])


async def _workspace_gc_loop(app: FastAPI):
    """Background task: destroy expired workspaces, pause idle ones.

    Runs every WORKSPACE_GC_INTERVAL_SEC (default 300s). Errors per-workspace
    are logged and skipped — one bad row should not stop the loop. The DB
    status is always brought to 'destroyed' even if the backend call fails,
    so we don't retry forever on a lost container.
    """
    print(f"[gc] starting (interval={WORKSPACE_GC_INTERVAL_SEC}s)", flush=True)
    while True:
        try:
            await asyncio.sleep(WORKSPACE_GC_INTERVAL_SEC)

            try:
                backend = get_backend()
            except SandboxError as e:
                print(f"[gc] backend unavailable, skipping: {e}", flush=True)
                continue

            # 1. Destroy expired (running or paused).
            with app.state.pool.connection() as conn:
                expired = conn.execute(
                    "SELECT id, backend_ref FROM workspaces "
                    "WHERE status IN ('running', 'paused') AND expires_at < now()",
                ).fetchall()
            for ws_id, be_ref in expired:
                try:
                    backend.destroy(be_ref)
                except Exception as e:
                    print(f"[gc] destroy {ws_id} failed: {e!r}", flush=True)
                with app.state.pool.connection() as conn:
                    conn.execute(
                        "UPDATE workspaces SET status='destroyed' WHERE id=%s",
                        (ws_id,),
                    )

            # 2. Pause running workspaces idle past the cutoff.
            with app.state.pool.connection() as conn:
                idle = conn.execute(
                    f"""
                    SELECT id, backend_ref FROM workspaces
                    WHERE status = 'running'
                      AND last_used_at < now() - interval '{WORKSPACE_IDLE_PAUSE_MIN} minutes'
                    """,
                ).fetchall()
            for ws_id, be_ref in idle:
                try:
                    backend.pause(be_ref)
                    with app.state.pool.connection() as conn:
                        conn.execute(
                            "UPDATE workspaces SET status='paused' WHERE id=%s",
                            (ws_id,),
                        )
                except Exception as e:
                    print(f"[gc] pause {ws_id} failed: {e!r}", flush=True)

            if expired or idle:
                print(
                    f"[gc] destroyed={len(expired)} paused={len(idle)}",
                    flush=True,
                )

        except asyncio.CancelledError:
            print("[gc] shutting down", flush=True)
            raise
        except Exception as e:
            # Don't crash the loop on unexpected errors — log and keep going.
            print(f"[gc] loop error: {e!r}", flush=True)


def _workspace_row_to_dict(row) -> dict:
    return {
        "id": str(row[0]),
        "session_id": str(row[1]),
        "backend": row[2],
        "status": row[3],
        "created_at": row[4].isoformat(),
        "last_used_at": row[5].isoformat(),
        "expires_at": row[6].isoformat(),
    }


def _select_workspace(conn, workspace_id: str, user_id: str):
    return conn.execute(
        """
        SELECT id, session_id, backend, status, created_at, last_used_at, expires_at,
               backend_ref
        FROM workspaces
        WHERE id = %s AND user_id = %s
        """,
        (workspace_id, user_id),
    ).fetchone()


def _bump_workspace_usage(conn, workspace_id: str) -> None:
    """Push last_used_at and expires_at forward — called on every use."""
    conn.execute(
        f"""
        UPDATE workspaces
        SET last_used_at = now(),
            expires_at   = now() + interval '{WORKSPACE_TTL_HOURS} hours'
        WHERE id = %s
        """,
        (workspace_id,),
    )


def _ensure_workspace_for_session(user_id: str, session_id: str) -> tuple[dict, str]:
    """Lazy-create or revive a workspace for a session.

    Returns `(row_dict, backend_ref)`. If an existing workspace is still healthy
    on the backend, reuses it (auto-resumes if paused). Otherwise enforces the
    per-user concurrency cap and creates a fresh one.

    Raises HTTPException(429) on cap, HTTPException(500) on backend failures.
    Caller is responsible for having already verified session ownership.
    """
    with app.state.pool.connection() as conn:
        existing = conn.execute(
            """
            SELECT id, backend, backend_ref, status
            FROM workspaces
            WHERE session_id = %s AND user_id = %s AND status != 'destroyed'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (session_id, user_id),
        ).fetchone()

    backend = get_backend()

    if existing:
        ws_id, _be, be_ref, _row_status = existing
        try:
            actual = backend.status(be_ref)
        except SandboxError:
            actual = "destroyed"

        if actual != "destroyed":
            if actual == "paused":
                try:
                    backend.resume(be_ref)
                except SandboxError as e:
                    raise HTTPException(500, f"Failed to resume workspace: {e}")
            with app.state.pool.connection() as conn:
                conn.execute(
                    f"""
                    UPDATE workspaces
                    SET status='running',
                        last_used_at=now(),
                        expires_at=now() + interval '{WORKSPACE_TTL_HOURS} hours'
                    WHERE id=%s
                    """,
                    (ws_id,),
                )
                row = conn.execute(
                    """
                    SELECT id, session_id, backend, status, created_at,
                           last_used_at, expires_at
                    FROM workspaces WHERE id = %s
                    """,
                    (ws_id,),
                ).fetchone()
            return _workspace_row_to_dict(row), be_ref

        # Backend lost it — mark stale, fall through to fresh create.
        with app.state.pool.connection() as conn:
            conn.execute(
                "UPDATE workspaces SET status='destroyed' WHERE id=%s",
                (ws_id,),
            )

    # Per-user concurrency cap before allocating a new sandbox.
    # (Reusing an existing workspace above skips this check by design — the
    # user isn't adding to their footprint, they're using what they have.)
    with app.state.pool.connection() as conn:
        active = _count_active_workspaces(conn, user_id)
    if active >= WORKSPACE_MAX_PER_USER:
        raise HTTPException(
            429,
            f"Workspace limit reached ({active}/{WORKSPACE_MAX_PER_USER}). "
            f"Destroy an existing workspace before creating a new one.",
        )

    backend_name = os.environ.get("SANDBOX_BACKEND", "docker").strip().lower()
    try:
        backend_ref = backend.create(user_id=user_id, session_id=session_id)
    except SandboxError as e:
        raise HTTPException(500, f"Failed to create workspace: {e}")

    try:
        with app.state.pool.connection() as conn:
            row = conn.execute(
                f"""
                INSERT INTO workspaces
                    (user_id, session_id, backend, backend_ref, expires_at)
                VALUES (%s, %s, %s, %s, now() + interval '{WORKSPACE_TTL_HOURS} hours')
                RETURNING id, session_id, backend, status, created_at,
                          last_used_at, expires_at
                """,
                (user_id, session_id, backend_name, backend_ref),
            ).fetchone()
    except Exception:
        # DB insert failed after backend.create succeeded — destroy the
        # container so we don't leak compute.
        try:
            backend.destroy(backend_ref)
        except Exception:
            pass
        raise

    # Fresh workspace — clone the linked repo or init an empty git repo so
    # rollback works from minute one. Best-effort: a failure here leaves the
    # container alive and the agent can re-run setup commands itself.
    try:
        _bootstrap_workspace(user_id, session_id, backend_ref)
    except Exception as e:
        print(f"[workspace-bootstrap] {e!r}", flush=True)

    return _workspace_row_to_dict(row), backend_ref


# Token-safe characters for git path components — letters, digits, `._-/`.
# Used to refuse interpolating user-controlled values into shell commands
# unless they're entirely benign.
_SAFE_GIT_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _bootstrap_workspace(user_id: str, session_id: str, backend_ref: str) -> None:
    """Initialise a freshly-created workspace.

    Linked sessions get `git clone` of their GitHub repo (token embedded in the
    clone URL, then immediately stripped so `git remote -v` doesn't leak it).
    Unlinked sessions get `git init` + an initial empty commit, so HEAD exists
    and rollback can target it from the first user file write onward.
    """
    backend = get_backend()

    with app.state.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT s.github_owner, s.github_repo, s.github_branch,
                   gc.token, gc.github_username
            FROM sessions s
            LEFT JOIN github_credentials gc ON gc.user_id = s.user_id
            WHERE s.id = %s AND s.user_id = %s
            """,
            (session_id, user_id),
        ).fetchone()
    if not row:
        return

    import shlex

    owner, repo, branch, token, github_username = row
    branch = branch or "main"
    user_name = github_username or "Agent"
    user_email = f"{github_username or 'agent'}@local"

    # URL components are interpolated into a clone URL — strict alphanumeric
    # check to defend against shell injection via owner/repo/branch.
    for v, label in [(owner, "owner"), (repo, "repo"), (branch, "branch")]:
        if v and not _SAFE_GIT_RE.match(str(v)):
            print(f"[workspace-bootstrap] refusing unsafe {label}: {v!r}", flush=True)
            owner = repo = None  # fall through to empty init

    # user.name / user.email go into single-quoted shell strings; shlex.quote
    # wraps them safely so any payload becomes an inert literal.
    name_q = shlex.quote(user_name)
    email_q = shlex.quote(user_email)

    if owner and repo and token:
        # Clone via temporary auth URL, then strip the token from the remote.
        # The PAT lives only in the in-memory cmd string and the immediate
        # git network call — never in `git remote -v` afterward.
        auth_url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
        public_url = f"https://github.com/{owner}/{repo}.git"
        cmd = (
            "set -e; "
            "shopt -s dotglob nullglob; "
            "rm -rf /workspace/* /workspace/.[!.]* 2>/dev/null || true; "
            "mkdir -p /tmp/acm-clone && rm -rf /tmp/acm-clone/*; "
            f"if git clone --branch {branch} --depth 50 {auth_url} /tmp/acm-clone 2>&1; then :; "
            f"else git clone --depth 50 {auth_url} /tmp/acm-clone 2>&1; fi; "
            "mv /tmp/acm-clone/* /tmp/acm-clone/.[!.]* /workspace/ 2>/dev/null || true; "
            "rm -rf /tmp/acm-clone; "
            "cd /workspace; "
            f"git remote set-url origin {public_url}; "
            f"git config user.name {name_q}; "
            f"git config user.email {email_q}"
        )
        try:
            r = backend.exec(backend_ref, cmd, cwd="/", timeout=180)
            if not r.ok:
                # stderr is already PAT-redacted by sandbox_client.
                print(
                    f"[workspace-bootstrap] clone failed exit={r.exit_code}: "
                    f"{r.stderr[:300]}",
                    flush=True,
                )
        except Exception as e:
            print(f"[workspace-bootstrap] clone exception: {e!r}", flush=True)
        return

    # Unlinked, or PAT missing for a linked session — init empty.
    cmd = (
        "set -e; cd /workspace; "
        "if [ ! -d .git ]; then git init -q -b main; fi; "
        f"git config user.name {name_q}; "
        f"git config user.email {email_q}; "
        "if ! git rev-parse HEAD >/dev/null 2>&1; then "
        "  git commit -q --allow-empty -m 'Initial commit'; "
        "fi"
    )
    try:
        r = backend.exec(backend_ref, cmd, cwd="/workspace", timeout=30)
        if not r.ok:
            print(
                f"[workspace-bootstrap] init failed exit={r.exit_code}: "
                f"{r.stderr[:300]}",
                flush=True,
            )
    except Exception as e:
        print(f"[workspace-bootstrap] init exception: {e!r}", flush=True)


def _sync_workspace_commits(
    user_id: str, session_id: str, workspace_id: str, backend_ref: str
) -> None:
    """Mirror the workspace's recent `git log` into `workspace_commits`.

    The workspace's git history is the source of truth; this table is a
    queryable mirror that drives the History UI and the undo endpoint.
    Idempotent — `UNIQUE (workspace_id, sha)` + `ON CONFLICT DO NOTHING`
    means safe to call after every chat turn even if nothing changed.
    """
    if not (workspace_id and backend_ref):
        return
    try:
        result = get_backend().exec(
            backend_ref,
            "git -C /workspace log --max-count=50 --format='%H%x09%s' 2>/dev/null || true",
            timeout=10,
        )
    except SandboxError as e:
        print(f"[commit-sync] exec failed: {e}", flush=True)
        return
    if not result.ok or not result.stdout.strip():
        return

    rows = []
    for line in result.stdout.splitlines():
        if "\t" not in line:
            continue
        sha, msg = line.split("\t", 1)
        sha, msg = sha.strip(), msg.strip()
        if sha and msg:
            rows.append((sha, msg))
    if not rows:
        return

    try:
        inserted: list[tuple[str, str]] = []
        with app.state.pool.connection() as conn:
            # Insert oldest-first so the highest serial id is the newest
            # commit — letting `ORDER BY id DESC` give a meaningful newest-
            # first feed in the UI.
            for sha, msg in reversed(rows):
                res = conn.execute(
                    """
                    INSERT INTO workspace_commits
                        (workspace_id, session_id, user_id, sha, message)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (workspace_id, sha) DO NOTHING
                    RETURNING sha, message
                    """,
                    (workspace_id, session_id, user_id, sha, msg),
                ).fetchone()
                if res:
                    inserted.append((res[0], res[1]))
        # Broadcast a single 'commits' event with what's new so the UI can
        # decide whether to refresh the history panel without polling.
        if inserted:
            try:
                event_bus.publish(
                    f"session:{session_id}",
                    {
                        "type": "commits",
                        "added": [{"sha": s, "message": m} for s, m in inserted],
                    },
                )
            except Exception as e:
                print(f"[event_bus] commits publish failed: {e!r}", flush=True)
    except Exception as e:
        print(f"[commit-sync] DB upsert failed: {e!r}", flush=True)


@app.post("/workspaces")
def create_or_get_workspace(
    req: CreateWorkspaceRequest,
    user_id: str = Depends(get_current_user),
):
    """Lazy-create a workspace for a session. If one already exists and is
    running or paused, return it (auto-resume if paused). If the row exists
    but the backend has lost it, mark stale and create a new one."""
    with app.state.pool.connection() as conn:
        _verify_session(conn, user_id, req.session_id)
    row, _ = _ensure_workspace_for_session(user_id, req.session_id)
    return row


@app.get("/workspaces/{workspace_id}")
def get_workspace(workspace_id: str, user_id: str = Depends(get_current_user)):
    with app.state.pool.connection() as conn:
        row = _select_workspace(conn, workspace_id, user_id)
    if not row:
        raise HTTPException(404, "Workspace not found")

    backend = get_backend()
    try:
        actual = backend.status(row[7])  # backend_ref
    except SandboxError:
        actual = "destroyed"

    # Reconcile drift between DB and backend.
    if actual != row[3]:
        with app.state.pool.connection() as conn:
            conn.execute(
                "UPDATE workspaces SET status=%s WHERE id=%s",
                (actual, workspace_id),
            )
            row = _select_workspace(conn, workspace_id, user_id)

    # Bump usage on every GET so polling the UI keeps the workspace alive.
    if actual != "destroyed":
        with app.state.pool.connection() as conn:
            _bump_workspace_usage(conn, workspace_id)
            row = _select_workspace(conn, workspace_id, user_id)

    return _workspace_row_to_dict(row)


@app.delete("/workspaces/{workspace_id}")
def delete_workspace(workspace_id: str, user_id: str = Depends(get_current_user)):
    with app.state.pool.connection() as conn:
        row = _select_workspace(conn, workspace_id, user_id)
    if not row:
        raise HTTPException(404, "Workspace not found")

    backend_ref = row[7]
    backend = get_backend()
    try:
        backend.destroy(backend_ref)
    except SandboxError as e:
        raise HTTPException(500, f"Failed to destroy workspace: {e}")

    with app.state.pool.connection() as conn:
        conn.execute(
            "UPDATE workspaces SET status='destroyed' WHERE id=%s",
            (workspace_id,),
        )
    return {"ok": True}


@app.post("/sessions/{session_id}/files")
async def upload_files(
    session_id: str,
    files: List[UploadFile] = File(...),
    user_id: str = Depends(get_current_user),
):
    with app.state.pool.connection() as conn:
        _verify_session(conn, user_id, session_id)

    bucket = get_bucket()
    saved = []
    for f in files:
        name = _safe_filename(f.filename or "unnamed")
        data = await f.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                413,
                f"{name} exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit",
            )
        key = file_key(user_id, session_id, name)
        try:
            bucket.upload(
                path=key,
                file=data,
                file_options={
                    "content-type": f.content_type or "application/octet-stream",
                    "upsert": "true",
                },
            )
        except Exception as e:
            raise HTTPException(500, f"Upload failed for {name}: {e}")
        saved.append({"name": name, "size": len(data)})
    return {"saved": saved}


def _active_workspace_ref(user_id: str, session_id: str) -> Optional[str]:
    """Return the live workspace's backend_ref for this session, or None.

    Unlike `_ensure_workspace_for_session`, this is a read-only lookup —
    it never provisions a new workspace, so cheap-to-call from /files
    endpoints that shouldn't spin up infra just to render the sidebar.
    """
    with app.state.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT backend_ref FROM workspaces
            WHERE session_id = %s AND user_id = %s AND status != 'destroyed'
            ORDER BY created_at DESC LIMIT 1
            """,
            (session_id, user_id),
        ).fetchone()
    return row[0] if row else None


def _list_workspace_files(backend_ref: str) -> list[dict]:
    """Top-level non-hidden files in /workspace, as `[{name, size}]`."""
    try:
        result = get_backend().exec(
            backend_ref,
            "find . -maxdepth 1 -type f -not -name '.*' -printf '%s\\t%f\\n' | sort -k2",
            cwd="/workspace",
            timeout=10,
        )
    except SandboxError as e:
        print(f"[list_files] workspace list failed: {e}", flush=True)
        return []
    if not result.ok:
        return []
    files = []
    for line in result.stdout.splitlines():
        if "\t" not in line:
            continue
        size_s, name = line.split("\t", 1)
        try:
            files.append({"name": name.strip(), "size": int(size_s.strip())})
        except ValueError:
            continue
    return files


@app.get("/sessions/{session_id}/files")
def list_files(
    session_id: str,
    user_id: str = Depends(get_current_user),
):
    """Merged listing of the session's files.

    Workspace files (the agent's live working copy) come first; user-uploaded
    S3 attachments come second, with duplicates suppressed in favour of the
    workspace copy. Each entry includes a `source` field so the UI can
    badge them differently if it wants to.
    """
    with app.state.pool.connection() as conn:
        _verify_session(conn, user_id, session_id)

    out: list[dict] = []
    seen: set[str] = set()

    backend_ref = _active_workspace_ref(user_id, session_id)
    if backend_ref:
        for f in _list_workspace_files(backend_ref):
            out.append(
                {
                    "name": f["name"],
                    "size": f["size"],
                    "modified_at": None,
                    "source": "workspace",
                }
            )
            seen.add(f["name"])

    bucket = get_bucket()
    try:
        items = bucket.list(session_prefix(user_id, session_id))
    except Exception as e:
        if not out:
            raise HTTPException(500, f"List failed: {e}")
        # Workspace listing already succeeded; S3 failure is non-fatal.
        items = []
    for it in items or []:
        if not it.get("id"):
            continue
        name = it.get("name")
        if not name or name in seen:
            continue
        if name.startswith(".acmdiff."):
            continue  # hidden diff sidecar written by write_project_file
        meta = it.get("metadata") or {}
        out.append(
            {
                "name": name,
                "size": meta.get("size", 0),
                "modified_at": it.get("updated_at") or it.get("created_at"),
                "source": "s3",
            }
        )
    return out


@app.delete("/sessions/{session_id}/files/{filename}")
def delete_file(
    session_id: str,
    filename: str,
    user_id: str = Depends(get_current_user),
):
    with app.state.pool.connection() as conn:
        _verify_session(conn, user_id, session_id)

    name = _safe_filename(filename)

    # Workspace removal — with an auto-commit so the deletion lands in
    # workspace_commits and is revertable like any other change.
    backend_ref = _active_workspace_ref(user_id, session_id)
    if backend_ref:
        try:
            get_backend().exec(
                backend_ref,
                "set -e; cd /workspace; "
                "if [ -f \"$ACM_FILE\" ]; then "
                "  rm -- \"$ACM_FILE\"; "
                "  if [ -d .git ]; then "
                "    git add -- \"$ACM_FILE\" 2>/dev/null || true; "
                "    git commit -q -m \"Agent: deleted $ACM_FILE\" 2>/dev/null || true; "
                "  fi; "
                "fi",
                env={"ACM_FILE": name},
                timeout=10,
            )
        except SandboxError as e:
            print(f"[delete_file] workspace remove failed: {e}", flush=True)

    # Always also remove from S3 so user-uploaded attachments with the same
    # name don't linger after a delete.
    bucket = get_bucket()
    try:
        bucket.remove([file_key(user_id, session_id, name)])
    except Exception as e:
        if not is_not_found(e):
            print(f"[delete_file] S3 remove failed: {e}", flush=True)
    return {"ok": True}


MAX_VIEW_BYTES = 1_000_000  # 1 MB cap for the in-browser viewer


@app.get("/sessions/{session_id}/files/{filename}")
def read_file_content(
    session_id: str,
    filename: str,
    user_id: str = Depends(get_current_user),
):
    with app.state.pool.connection() as conn:
        _verify_session(conn, user_id, session_id)

    name = _safe_filename(filename)

    # Workspace first (the live working copy), S3 second (legacy attachments).
    data: bytes | None = None
    backend_ref = _active_workspace_ref(user_id, session_id)
    if backend_ref:
        try:
            data = get_backend().read_file(backend_ref, f"/workspace/{name}")
        except SandboxNotFoundError:
            data = None
        except SandboxError as e:
            print(f"[read_file] workspace read failed: {e}", flush=True)

    if data is None:
        bucket = get_bucket()
        try:
            data = bucket.download(file_key(user_id, session_id, name))
        except Exception as e:
            if is_not_found(e):
                raise HTTPException(404, "File not found")
            raise HTTPException(500, f"Download failed: {e}")

    size = len(data)
    truncated = size > MAX_VIEW_BYTES
    if truncated:
        data = data[:MAX_VIEW_BYTES]
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(415, "Not a UTF-8 text file")
    return {
        "name": name,
        "size": size,
        "truncated": truncated,
        "content": content,
    }


@app.get("/sessions/{session_id}/files/{filename}/diff")
def read_file_diff(
    session_id: str,
    filename: str,
    user_id: str = Depends(get_current_user),
):
    """Return the unified diff of the most recent write to an S3 (chat-session)
    file, stored as a hidden ``.acmdiff.`` sidecar by write_project_file. Lets
    the UI render a red/green diff for files that have no git commit. 404 when
    no diff was recorded (e.g. the file was uploaded, never agent-written)."""
    with app.state.pool.connection() as conn:
        _verify_session(conn, user_id, session_id)
    name = _safe_filename(filename)
    try:
        data = get_bucket().download(
            file_key(user_id, session_id, f".acmdiff.{name}")
        )
    except Exception as e:
        if is_not_found(e):
            raise HTTPException(404, "No diff recorded for this file")
        raise HTTPException(500, f"Download failed: {e}")
    return {"diff": data.decode("utf-8", errors="replace")}


@app.post("/title")
def make_title(req: TitleRequest, _user_id: str = Depends(get_current_user)):
    """Generate a 3–7 word topic title from the given text. Best-effort —
    the frontend should fall back to a heuristic if this fails."""
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "Empty text")
    prompt = (
        "You produce short topic labels for chat conversations. "
        "Read the user's message below and reply with a 3 to 7 word title that "
        "captures the topic — no quotes, no trailing punctuation, no preface, "
        "title case. Title only.\n\n"
        f"Message:\n{text[:1500]}"
    )
    try:
        result = _build_model(DEFAULT_MODEL).invoke([HumanMessage(content=prompt)])
        raw = (getattr(result, "content", "") or "").strip()
        # Take first non-empty line, strip wrapping quotes/punct.
        line = next((l.strip() for l in raw.splitlines() if l.strip()), "")
        line = line.strip('"').strip("'").strip().rstrip(".!?,;:")
        # Clamp to 7 words.
        words = line.split()
        if not words:
            raise ValueError("Empty title")
        title = " ".join(words[:7])
        return {"title": title}
    except Exception as e:
        msg = str(e) or e.__class__.__name__
        if "429" in msg or "rate" in msg.lower():
            raise HTTPException(429, "Rate-limited")
        raise HTTPException(500, f"Title generation failed: {msg[:200]}")


# ── GitHub integration ─────────────────────────────────────────────────────

@app.get("/github/status")
def github_status(user_id: str = Depends(get_current_user)):
    """Returns the connected GitHub username, or null if not connected."""
    with app.state.pool.connection() as conn:
        username = github_client.get_username(conn, user_id)
    return {"connected": bool(username), "username": username}


@app.post("/github/token")
def save_github_token(
    req: GithubTokenRequest,
    user_id: str = Depends(get_current_user),
):
    """Save and verify a GitHub Personal Access Token."""
    token = (req.token or "").strip()
    if not token:
        raise HTTPException(400, "Token is empty")
    try:
        with app.state.pool.connection() as conn:
            username = github_client.save_token(conn, user_id, token)
    except ValueError as e:
        raise HTTPException(401, str(e))
    return {"connected": True, "username": username}


@app.delete("/github/token")
def delete_github_token(user_id: str = Depends(get_current_user)):
    with app.state.pool.connection() as conn:
        github_client.delete_token(conn, user_id)
    return {"connected": False}


@app.post("/github/repo")
def create_github_repo(
    req: CreateGithubRepoRequest,
    user_id: str = Depends(get_current_user),
):
    """Create a new repo on the user's GitHub account using their stored PAT.

    Verifies the token has the 'repo' scope before attempting creation so the
    user gets a clear "re-paste your PAT" prompt instead of an opaque GitHub
    rejection. Returns repo metadata the project-creation flow needs to link
    the session.
    """
    with app.state.pool.connection() as conn:
        token = github_client.get_token(conn, user_id)
    if not token:
        raise HTTPException(
            400,
            "No GitHub token connected. Connect a GitHub PAT first.",
        )

    try:
        scope_info = github_client.verify_token_scopes(token)
    except ValueError as e:
        raise HTTPException(401, str(e))

    allowed, reason = github_client.can_create_repos(scope_info)
    if not allowed:
        raise HTTPException(403, reason)

    try:
        info = github_client.create_repo(
            token, req.name, private=req.private
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    return info


@app.post("/sessions/{session_id}/history/{commit_id}/revert")
def revert_workspace_commit(
    session_id: str,
    commit_id: int,
    user_id: str = Depends(get_current_user),
):
    """Undo a previous workspace commit by running `git revert` inside the
    sandbox. Creates a new "Revert ..." commit on top of HEAD; the original
    row is stamped `reverted_at` and the new revert commit will appear in
    history on its own (via `_sync_workspace_commits`).
    """
    with app.state.pool.connection() as conn:
        _verify_session(conn, user_id, session_id)
        row = conn.execute(
            """
            SELECT wc.sha, wc.message, wc.reverted_at,
                   w.id, w.backend_ref, w.status
            FROM workspace_commits wc
            JOIN workspaces w ON w.id = wc.workspace_id
            WHERE wc.id = %s AND wc.session_id = %s AND wc.user_id = %s
            """,
            (commit_id, session_id, user_id),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Commit not found")
    sha, _msg, reverted_at, ws_id, backend_ref, ws_status = row
    if reverted_at:
        raise HTTPException(409, "Commit already reverted")
    if ws_status == "destroyed":
        raise HTTPException(
            410, "The workspace this commit lived in is gone; cannot revert."
        )

    backend = get_backend()
    script = (
        "set -e\n"
        "cd /workspace\n"
        # `if !` deliberately bypasses `set -e` so we can clean up the
        # half-applied revert state on conflict before bailing.
        "if ! git revert --no-edit \"$ACM_SHA\"; then\n"
        "  git revert --abort 2>/dev/null || true\n"
        "  echo 'revert-failed'\n"
        "  exit 1\n"
        "fi\n"
        "git rev-parse --short HEAD\n"
    )
    try:
        result = backend.exec(
            backend_ref, script, env={"ACM_SHA": sha}, timeout=30,
        )
    except SandboxError as e:
        raise HTTPException(500, f"revert exec failed: {e}")
    if not result.ok:
        # git revert exits non-zero on merge conflict; we already aborted.
        err = (result.stderr or result.stdout or "").strip().splitlines()
        snippet = err[-1][:200] if err else "unknown error"
        raise HTTPException(
            409, f"git revert failed (likely conflict): {snippet}"
        )

    new_sha = (result.stdout or "").strip().splitlines()[-1]

    with app.state.pool.connection() as conn:
        conn.execute(
            "UPDATE workspace_commits SET reverted_at = now() WHERE id = %s",
            (commit_id,),
        )
    # Sync so the new revert commit appears in subsequent /history calls.
    try:
        _sync_workspace_commits(user_id, session_id, str(ws_id), backend_ref)
    except Exception as e:
        print(f"[revert] post-revert sync failed: {e!r}", flush=True)

    return {
        "ok": True,
        "reverted_sha": sha,
        "new_sha": new_sha,
    }


@app.get("/sessions/{session_id}/commits/{sha}/diff")
def get_commit_diff(
    session_id: str,
    sha: str,
    user_id: str = Depends(get_current_user),
):
    """Return the unified diff for a single commit in the session's workspace.

    Used by the file-edit card's `View diff` expander. Looks up the live
    workspace and runs `git show <sha>` inside it; returns the text body so
    the UI can render the +/- lines.
    """
    # Validate sha shape — only hex, reasonable length — to avoid shell
    # injection. (git itself would reject pathological inputs, but this keeps
    # the exec script body trivial.)
    if not re.match(r"^[A-Fa-f0-9]{4,40}$", sha):
        raise HTTPException(400, "invalid sha")

    with app.state.pool.connection() as conn:
        _verify_session(conn, user_id, session_id)
        row = conn.execute(
            """
            SELECT id, backend_ref, status
            FROM workspaces
            WHERE session_id = %s AND user_id = %s AND status != 'destroyed'
            ORDER BY created_at DESC LIMIT 1
            """,
            (session_id, user_id),
        ).fetchone()
    if not row:
        raise HTTPException(
            404,
            "No live workspace for this session; diff is only available while "
            "the workspace exists.",
        )
    _ws_id, backend_ref, _status = row

    try:
        result = get_backend().exec(
            backend_ref,
            "git -C /workspace show --no-color --format='' \"$ACM_SHA\"",
            env={"ACM_SHA": sha},
            timeout=10,
        )
    except SandboxError as e:
        raise HTTPException(500, f"diff exec failed: {e}")
    if not result.ok:
        snippet = (result.stderr or result.stdout or "").strip()[:200]
        raise HTTPException(404, f"git show failed: {snippet}")
    # Cap to a sane size so a huge commit doesn't blow up the UI.
    diff = result.stdout
    if len(diff) > 200_000:
        diff = diff[:200_000] + "\n[... diff truncated ...]"
    return {"sha": sha, "diff": diff}


@app.get("/sessions/{session_id}/threads/{thread_id}/stream")
async def stream_session_events(
    session_id: str,
    thread_id: str,
    token: str = Query(..., description="Supabase JWT (query-string because EventSource can't send headers)"),
):
    """Server-Sent Events feed for a single thread.

    Emits one event per:
      - new message recorded (`type=message`)
      - workspace commits synced (`type=commits`, scoped by session)
      - approval requests / status (`type=approval_*`, Wave 2)

    The stream stays open for the life of the EventSource connection; we
    don't return a Response object directly because StreamingResponse owns
    the lifecycle.
    """
    user_id = _verify_jwt(token)

    # Verify the user actually owns this thread before opening a channel.
    with app.state.pool.connection() as conn:
        _verify_thread(conn, user_id, session_id, thread_id)

    async def merged() -> AsyncIterator[bytes]:
        # Subscribe to both channels (thread for messages, session for
        # commits) and round-robin merge. Two independent queues so a busy
        # commit feed can't starve message delivery.
        thread_q = await event_bus.subscribe(f"thread:{thread_id}")
        session_q = await event_bus.subscribe(f"session:{session_id}")
        yield b": connected\n\n"
        # Keep getter tasks alive across iterations. Re-creating them inside
        # the loop leaks the pending one and races future events between
        # abandoned + fresh getters — events landing on an abandoned task
        # are silently lost.
        thread_task = asyncio.create_task(thread_q.get())
        session_task = asyncio.create_task(session_q.get())
        try:
            while True:
                done, _pending = await asyncio.wait(
                    [thread_task, session_task],
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=15,
                )
                if not done:
                    yield b": keepalive\n\n"
                    continue
                for task in done:
                    event = task.result()
                    yield f"data: {json.dumps(event)}\n\n".encode("utf-8")
                    if task is thread_task:
                        thread_task = asyncio.create_task(thread_q.get())
                    elif task is session_task:
                        session_task = asyncio.create_task(session_q.get())
        finally:
            for t in (thread_task, session_task):
                if not t.done():
                    t.cancel()
            await event_bus.unsubscribe(f"thread:{thread_id}", thread_q)
            await event_bus.unsubscribe(f"session:{session_id}", session_q)

    return StreamingResponse(
        merged(),
        media_type="text/event-stream",
        # Disable buffering on the way out so events actually flush in real time.
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/sessions/{session_id}/history")
def get_workspace_history(
    session_id: str,
    user_id: str = Depends(get_current_user),
):
    """Return the recent git-commit history for a project session.

    Backed by `workspace_commits`, which is synced from the workspace's
    `git log` at the end of every chat turn. Returns newest first.
    """
    with app.state.pool.connection() as conn:
        _verify_session(conn, user_id, session_id)
        rows = conn.execute(
            """
            SELECT id, sha, message, pushed_at, reverted_at, created_at
            FROM workspace_commits
            WHERE session_id = %s AND user_id = %s
            ORDER BY id DESC
            LIMIT 100
            """,
            (session_id, user_id),
        ).fetchall()
    return [
        {
            "id": int(r[0]),
            "sha": r[1],
            "message": r[2],
            "pushed_at": r[3].isoformat() if r[3] else None,
            "reverted_at": r[4].isoformat() if r[4] else None,
            "created_at": r[5].isoformat(),
            "status": (
                "reverted" if r[4] else "pushed" if r[3] else "local"
            ),
        }
        for r in rows
    ]


@app.get("/sessions/{session_id}/threads/{thread_id}/history")
def get_history(
    session_id: str,
    thread_id: str,
    user_id: str = Depends(get_current_user),
):
    with app.state.pool.connection() as conn:
        _verify_thread(conn, user_id, session_id, thread_id)
        rows = conn.execute(
            """
            SELECT id, role, content, tool_name, tool_calls_json
            FROM messages
            WHERE session_id = %s AND thread_id = %s AND user_id = %s
            ORDER BY id ASC
            """,
            (session_id, thread_id, user_id),
        ).fetchall()
    out = []
    for msg_id, role, content, tool_name, tool_calls_json in rows:
        m: dict = {"id": int(msg_id), "role": role, "content": content}
        if tool_name:
            m["tool_name"] = tool_name
        if tool_calls_json:
            m["tool_calls"] = (
                tool_calls_json
                if isinstance(tool_calls_json, list)
                else json.loads(tool_calls_json)
            )
        out.append(m)
    return out


def _estimate_tokens(text: str) -> int:
    """Rough OpenAI-style estimate: ~4 characters per token for English text."""
    if not text:
        return 0
    return max(1, len(text) // 4)


# ── Context-management observability ──────────────────────────────────────
#
# Two helpers used by the /context endpoint and by future strategies
# (tool-result trimming, summarisation, sliding window, sub-agent).
#
# `_compute_trajectory(message_rows)` turns the raw messages-table rows
# into a per-turn token series the UI can plot as a sparkline. A "turn"
# starts on a user message and includes every assistant / tool message
# that follows until the next user message.
#
# `_record_context_event(...)` appends one row to context_events.
# Strategies call this whenever they edit the in-flight message list
# so the UI can show the user *which* technique fired and how much it
# saved.


def _compute_trajectory(message_rows: List[tuple]) -> List[Dict[str, Any]]:
    """Group the SELECT rows of `messages` into per-turn token totals.

    Expected row shape (matches the SELECT in get_context):
      (id, role, content, tool_name, tool_calls_json,
       tokens, input_tokens, output_tokens, thinking_tokens, langgraph_id)
    """
    turns: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for row in message_rows:
        role = row[1]
        content = row[2] or ""
        recorded = int(row[5] or 0)
        in_tok = int(row[6] or 0)
        out_tok = int(row[7] or 0)
        cache_read = int(row[10] or 0) if len(row) > 10 else 0
        # Prefer the canonical `tokens` column when it's populated; fall
        # back to the rough estimator when the message predates the
        # token-recording columns or is a tool message that never had
        # them written.
        msg_tok = recorded if recorded > 0 else _estimate_tokens(content)
        if role == "user":
            # Push the previous turn, if any, before opening a new one.
            if current is not None:
                turns.append(current)
            current = {
                "turn": len(turns) + 1,
                "input_tokens": msg_tok + in_tok,
                "output_tokens": out_tok,
                "cache_read_tokens": cache_read,
                "messages": 1,
            }
        elif current is not None:
            # Assistant or tool message — counts as output for the turn.
            current["output_tokens"] += msg_tok + out_tok
            current["cache_read_tokens"] = current.get("cache_read_tokens", 0) + cache_read
            current["messages"] += 1
        else:
            # Edge case: leading non-user messages with no preceding
            # user. Lump them into a turn-0 bucket so the UI still
            # plots them rather than silently dropping.
            current = {
                "turn": 0,
                "input_tokens": 0,
                "output_tokens": msg_tok + out_tok,
                "messages": 1,
            }
    if current is not None:
        turns.append(current)
    cumulative = 0
    for t in turns:
        t["turn_tokens"] = t["input_tokens"] + t["output_tokens"]
        cumulative += t["turn_tokens"]
        t["cumulative_tokens"] = cumulative
    return turns


def _record_context_event(
    conn,
    user_id: str,
    session_id: str,
    thread_id: str,
    turn_index: int,
    edit_type: str,
    freed_tokens: int = 0,
    details: Optional[Dict[str, Any]] = None,
) -> int:
    """Append one row to `context_events`. Returns the new id.

    `edit_type` is a free-form string. Conventional values shipped by
    the strategies in CONTEXT_STRATEGIES_PLAN.md:
      - "tool_result_trimming"
      - "summarization"
      - "sliding_window"
      - "subagent_call"
      - "memory_write" / "memory_read"

    `details` is JSON-serialised and exposed to the UI verbatim — keep
    it small and explanatory ("cleared 12 tool results", a summary
    preview, the subagent's token totals, etc.).
    """
    row = conn.execute(
        """
        INSERT INTO context_events
            (user_id, session_id, thread_id, turn_index, edit_type,
             freed_tokens, details_json)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            user_id,
            session_id,
            thread_id,
            int(turn_index),
            edit_type,
            int(freed_tokens or 0),
            json.dumps(details) if details else None,
        ),
    ).fetchone()
    return int(row[0]) if row else 0


def _list_context_events(
    conn, user_id: str, session_id: str, thread_id: str
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, turn_index, edit_type, freed_tokens, details_json, created_at
          FROM context_events
         WHERE user_id = %s AND session_id = %s AND thread_id = %s
         ORDER BY id ASC
        """,
        (user_id, session_id, thread_id),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        details = r[4]
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except Exception:
                pass
        out.append(
            {
                "id": int(r[0]),
                "turn": int(r[1] or 0),
                "type": r[2],
                "freed_tokens": int(r[3] or 0),
                "details": details,
                "at": r[5].isoformat() if r[5] else None,
            }
        )
    return out


# Context window sizes for common models (in tokens). Used to compute %used.
_MODEL_CONTEXT_LIMITS = {
    "meta-llama/llama-3.3-70b-instruct:free": 131072,
    "z-ai/glm-4.5-air:free": 131072,
    "qwen/qwen-2.5-72b-instruct:free": 131072,
    "google/gemini-2.0-flash-exp:free": 1048576,
    "google/gemini-2.5-flash": 1048576,
    "openai/gpt-4o-mini": 128000,
    "anthropic/claude-haiku-4-5": 200000,
}


@app.get("/models")
def list_models(user_id: str = Depends(get_current_user)):
    """Return the list of OpenRouter `:free` models the UI can pick from.
    Cached server-side for 10 minutes."""
    items = _fetch_free_models()
    return {"default": DEFAULT_MODEL, "models": items}


@app.get("/context/strategies")
def list_context_strategies(user_id: str = Depends(get_current_user)):
    """Available context-management strategies + the current default.
    Wire-compatible with a future UI selector; for now the strategy can
    be overridden per request via ChatRequest.context_strategy.
    """
    strategies = [
        {
            "id": "tool_calling",
            "label": "Tool Calling",
            "summary": (
                "Classic ReAct loop — every tool is a separate "
                "LangChain tool, the model picks one per round."
            ),
        },
        {
            "id": "ts_code_mode",
            "label": "TypeScript Code Mode",
            "summary": (
                "Compact catalog in the prompt; describe_tools fetches "
                "TS interfaces on demand; execute_typescript runs one "
                "program per turn in a Deno isolate. Saves tokens and "
                "round-trips on multi-step tasks; needs Deno installed."
            ),
        },
    ]
    return {"default": DEFAULT_CONTEXT_STRATEGY, "strategies": strategies}


@app.get("/sessions/{session_id}/threads/{thread_id}/context")
def get_context(
    session_id: str,
    thread_id: str,
    model: Optional[str] = None,
    user_id: str = Depends(get_current_user),
):
    """Return what the LLM sees on the next turn: system prompt, message
    history, attached files, and approximate token usage."""
    with app.state.pool.connection() as conn:
        _verify_thread(conn, user_id, session_id, thread_id)
        rows = conn.execute(
            """
            SELECT id, role, content, tool_name, tool_calls_json,
                   tokens, input_tokens, output_tokens, thinking_tokens,
                   langgraph_id, cache_read_tokens
            FROM messages
            WHERE session_id = %s AND thread_id = %s AND user_id = %s
            ORDER BY id ASC
            """,
            (session_id, thread_id, user_id),
        ).fetchall()
        applied_edits = _list_context_events(conn, user_id, session_id, thread_id)
    trajectory = _compute_trajectory(rows)

    messages = []
    cache_read_total = 0
    est_total = 0      # content-based fallback: each message counted ONCE
    last_input = 0     # most recent model call's prompt size (whole context)
    last_output = 0
    last_cache_read = 0  # cache hit on that same most-recent call
    for row in rows:
        (
            row_id,
            role,
            content,
            tool_name,
            tool_calls_json,
            recorded_tokens,
            input_tok,
            output_tok,
            thinking_tok,
            langgraph_id,
            cache_read_tok,
        ) = row
        # Per-message OWN size for the fallback estimate — for an assistant
        # turn that's its completion (output_tokens), NOT `recorded_tokens`
        # (which is the whole-call total incl. the entire prior history;
        # summing those is exactly what blew the meter past the 1M limit).
        if role == "assistant" and output_tok and int(output_tok) > 0:
            own = int(output_tok)
        else:
            own = _estimate_tokens(content or "")
        est_total += own
        # Rows are ASC by id, so the last row with a real prompt count wins =
        # the most recent model call's view of the full context.
        if int(input_tok or 0) > 0:
            last_input = int(input_tok)
            last_output = int(output_tok or 0)
            last_cache_read = int(cache_read_tok or 0)
        cache_read_total += int(cache_read_tok or 0)
        # Per-message display value left as recorded (back-compat with the
        # message list); the meter total below no longer sums these.
        disp = (
            int(recorded_tokens)
            if recorded_tokens and recorded_tokens > 0
            else own
        )
        m = {
            "id": int(row_id),
            "role": role,
            "content": content,
            "tokens": disp,
            "input_tokens": int(input_tok or 0),
            "output_tokens": int(output_tok or 0),
            "thinking_tokens": int(thinking_tok or 0),
            "cache_read_tokens": int(cache_read_tok or 0),
            "has_langgraph_id": bool(langgraph_id),
        }
        if tool_name:
            m["tool_name"] = tool_name
        if tool_calls_json:
            m["tool_calls"] = (
                tool_calls_json
                if isinstance(tool_calls_json, list)
                else json.loads(tool_calls_json)
            )
        messages.append(m)

    sys_tokens = _estimate_tokens(SYSTEM_PROMPT)

    # Current context usage. A model call's `input_tokens` already counts the
    # ENTIRE prompt (system + full history + any images) exactly as the
    # provider tokenised it, so the truthful "tokens in the window right now"
    # is the most recent call's input + its output. We must NOT sum the
    # per-message `tokens` totals — each already includes all prior turns, so
    # summing multiply-counts the history (that's the >1M-token bug).
    if last_input > 0:
        total_tokens = last_input + last_output  # sys already included here
    else:
        total_tokens = est_total + sys_tokens  # fresh thread, no usage yet

    # Files in the session's bucket folder.
    files: list[dict] = []
    try:
        items = get_bucket().list(session_prefix(user_id, session_id))
        for it in items or []:
            if not it.get("id"):
                continue
            meta = it.get("metadata") or {}
            files.append({"name": it.get("name"), "size": meta.get("size", 0)})
    except Exception as e:
        print(f"[/context] could not list files: {e!r}", flush=True)

    model_name = (model or "").strip() or DEFAULT_MODEL
    if model_name in _MODEL_CONTEXT_LIMITS:
        context_limit = _MODEL_CONTEXT_LIMITS[model_name]
    else:
        # Fall back to the OpenRouter catalog if we haven't hard-coded this one.
        catalog = {m["id"]: m.get("context_length") or 0 for m in _fetch_free_models()}
        context_limit = catalog.get(model_name) or 128000

    return {
        "model": model_name,
        "context_limit": context_limit,
        "total_tokens": total_tokens,
        "percent_used": round(100 * total_tokens / context_limit, 2)
        if context_limit
        else 0,
        "system_prompt": SYSTEM_PROMPT,
        "system_tokens": sys_tokens,
        # Image-recall caching: prompt tokens served from cache on the most
        # recent model call (0 when caching is off / unsupported). This is
        # the cache hit for the *current* context — summing it across turns
        # would multiply-count (each turn re-sends + re-caches the history),
        # which is what produced the impossible ">100%" cache figure.
        "cache_read_tokens": last_cache_read,
        "messages": messages,
        "files": files,
        # PR #0 additions — empty until a strategy actually writes
        # context_events rows, but the keys are stable so the UI can
        # render the panels with "no edits yet" right away.
        "trajectory": trajectory,
        "applied_edits": applied_edits,
    }


@app.delete("/sessions/{session_id}/threads/{thread_id}/messages/{message_id}")
def delete_message(
    session_id: str,
    thread_id: str,
    message_id: int,
    model: Optional[str] = None,
    user_id: str = Depends(get_current_user),
):
    """Remove one message from the chat history.

    Deletes the row from `messages` AND, when the message has a known
    LangGraph id, surgically removes it from the agent's checkpoint state
    via RemoveMessage so the LLM won't see it on the next turn.

    Old messages saved before the langgraph_id column existed only get
    deleted from the display table — the agent's state will still include
    them. Callers can detect this from the returned `removed_from_state`
    field.
    """
    with app.state.pool.connection() as conn:
        _verify_thread(conn, user_id, session_id, thread_id)
        row = conn.execute(
            "SELECT langgraph_id FROM messages "
            "WHERE id = %s AND session_id = %s AND thread_id = %s AND user_id = %s",
            (message_id, session_id, thread_id, user_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Message not found")
        langgraph_id = row[0]
        conn.execute("DELETE FROM messages WHERE id = %s", (message_id,))

    removed_from_state = False
    if langgraph_id:
        try:
            agent = _get_agent(model)
            agent.update_state(
                {
                    "configurable": {
                        "thread_id": f"{user_id}:{session_id}",
                        "user_id": user_id,
                        "session_id": session_id,
                    }
                },
                {"messages": [RemoveMessage(id=langgraph_id)]},
            )
            removed_from_state = True
        except Exception as e:
            # DB row is already gone; log and continue so the UI still sees
            # the message disappear. The agent's view will catch up if the
            # state ever gets fully rebuilt.
            print(
                f"[delete_message] LangGraph update_state failed for "
                f"id={langgraph_id}: {e!r}",
                flush=True,
            )

    return {"ok": True, "removed_from_state": removed_from_state}


@app.get("/sessions/{session_id}/threads/{thread_id}/messages/{message_id}/images")
def get_message_images(
    session_id: str,
    thread_id: str,
    message_id: int,
    model: Optional[str] = None,
    user_id: str = Depends(get_current_user),
):
    """Return the visual-method page images for one tool-result message.

    The rasterised PNGs live in the LangGraph checkpoint (in the messages
    table they're flattened to an ``[image]`` marker). We map the display
    row to its checkpoint message via ``langgraph_id`` and return each page
    as a data URL the UI can drop straight into an ``<img>``.
    """
    with app.state.pool.connection() as conn:
        _verify_thread(conn, user_id, session_id, thread_id)
        row = conn.execute(
            "SELECT langgraph_id, tool_name FROM messages "
            "WHERE id = %s AND session_id = %s AND thread_id = %s AND user_id = %s",
            (message_id, session_id, thread_id, user_id),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Message not found")
    langgraph_id, tool_name = row
    if not langgraph_id:
        return {"images": [], "count": 0, "tool": tool_name}

    try:
        agent = _get_agent(model)
        state = agent.get_state(
            {
                "configurable": {
                    "thread_id": f"{user_id}:{session_id}",
                    "user_id": user_id,
                    "session_id": session_id,
                }
            }
        )
        messages = (state.values or {}).get("messages", []) or []
    except Exception as e:
        raise HTTPException(500, f"Could not read conversation state: {e}")

    target = next(
        (m for m in messages if getattr(m, "id", None) == langgraph_id), None
    )
    images: List[str] = []
    content = getattr(target, "content", None) if target is not None else None
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "image_url":
                url = (b.get("image_url") or {}).get("url", "")
                if url:
                    images.append(url)
            elif b.get("type") == "image":
                src = b.get("source") or {}
                if src.get("type") == "base64" and src.get("data"):
                    mt = src.get("media_type", "image/png")
                    images.append(f"data:{mt};base64,{src['data']}")
    return {"images": images, "count": len(images), "tool": tool_name}


# ─── relevance pruning (task-aware removal suggestions) ──────────────────


def _thread_lc_messages(conn, user_id: str, session_id: str, thread_id: str):
    """Load a thread's messages as ``List[BaseMessage]`` for the relevance
    engine, plus a parallel ``meta`` list (``{db_id, langgraph_id, role}``) so an
    episode's member indices map back onto rows the delete path can remove."""
    rows = conn.execute(
        """
        SELECT id, role, content, tool_name, tool_calls_json, langgraph_id
          FROM messages
         WHERE session_id = %s AND thread_id = %s AND user_id = %s
         ORDER BY id ASC
        """,
        (session_id, thread_id, user_id),
    ).fetchall()
    lc: List[Any] = []
    meta: List[Dict[str, Any]] = []
    for row_id, role, content, tool_name, _tool_calls_json, langgraph_id in rows:
        text = content or ""
        r = (role or "").lower()
        # Relevance only reads message *text* + role; tool_call structures aren't
        # needed for segmentation/judging, so we never reconstruct them (the
        # DB's tool_calls_json isn't in LangChain's {name,args,id} shape and
        # would fail AIMessage validation).
        if r in ("assistant", "ai"):
            msg: Any = AIMessage(content=text)
        elif r == "tool":
            msg = ToolMessage(content=text, tool_call_id="", name=tool_name or None)
        elif r == "system":
            msg = SystemMessage(content=text)
        else:
            msg = HumanMessage(content=text)
        lc.append(msg)
        meta.append({"db_id": int(row_id), "langgraph_id": langgraph_id, "role": r})
    return lc, meta


def _resolve_relevance_cfg(conn, user_id: str, session_id: str):
    """Resolved relevance_pruning config for this thread (or schema defaults)."""
    from context_profiles import resolve_profile  # lazy

    try:
        profile, _name = resolve_profile(
            conn, user_id=user_id, session_id=session_id
        )
        return getattr(profile.context_management, "relevance_pruning", None)
    except Exception as e:
        print(f"[relevance] profile resolve failed: {e!r}", flush=True)
        return None


@app.post("/sessions/{session_id}/threads/{thread_id}/relevance/suggest")
def relevance_suggest(
    session_id: str,
    thread_id: str,
    model: Optional[str] = None,
    user_id: str = Depends(get_current_user),
):
    """Audit this thread and return per-episode KEEP/SUMMARIZE/DROP suggestions.
    Suggest-only — nothing is removed. Each suggestion lists ``member_ids`` (DB
    message ids) so the UI can drop a whole episode via /relevance/apply."""
    from relevance import suggest_removals  # lazy

    with app.state.pool.connection() as conn:
        _verify_thread(conn, user_id, session_id, thread_id)
        lc, meta = _thread_lc_messages(conn, user_id, session_id, thread_id)
        cfg = _resolve_relevance_cfg(conn, user_id, session_id)
    if not lc:
        return {"thread_id": thread_id, "suggestions": [], "info": {}}

    keep_recent = int(getattr(cfg, "keep_recent", 3) if cfg else 3)
    mode = str(getattr(cfg, "mode", "judge") if cfg else "judge")
    arbitration = str(getattr(cfg, "arbitration", "safest") if cfg else "safest")
    drop_t = float(getattr(cfg, "drop_threshold", 0.35) if cfg else 0.35)
    summ_t = float(getattr(cfg, "summarize_threshold", 0.6) if cfg else 0.6)

    need_judge = mode in ("judge", "ensemble")
    need_encoder = mode in ("encoder", "ensemble")

    judge = None
    if need_judge:
        slug = (getattr(cfg, "judge_model", None) if cfg else None) or model or DEFAULT_MODEL
        try:
            judge = _build_model(slug)
        except Exception as e:
            print(f"[relevance] judge model {slug!r} failed: {e!r}", flush=True)
            if mode == "judge":
                raise HTTPException(502, f"judge model unavailable: {e}")

    encoder = None
    if need_encoder:
        try:
            from relevance_encoder import EncoderSuggester  # lazy (heavy deps)

            path = (getattr(cfg, "encoder_path", None) if cfg else None) or os.getenv(
                "ACM_ENCODER_PATH"
            )
            encoder = EncoderSuggester(
                path or None, drop_threshold=drop_t, summarize_threshold=summ_t
            )
        except Exception as e:
            print(f"[relevance] encoder init failed: {e!r}", flush=True)
            if mode == "encoder":
                raise HTTPException(502, f"encoder unavailable: {e}")

    suggestions, info, episodes = suggest_removals(
        lc,
        keep_recent=keep_recent,
        mode=mode,
        arbitration=arbitration,
        judge_client=judge,
        encoder=encoder,
        return_episodes=True,
    )
    # Capture features (task + episode text + model label) for the training
    # loop, unless the profile turned logging off.
    if getattr(cfg, "feedback_logging", True):
        try:
            from relevance import active_task, build_audit_rows, record_audit  # lazy

            record_audit(
                build_audit_rows(
                    episodes,
                    suggestions,
                    task=active_task(lc),
                    conv=thread_id,
                    surface="website",
                )
            )
        except Exception as e:
            print(f"[relevance] audit log failed: {e!r}", flush=True)
    out: List[Dict[str, Any]] = []
    for s in suggestions:
        members = [meta[i] for i in s.member_indices if 0 <= i < len(meta)]
        out.append(
            {
                **s.to_dict(),
                "member_ids": [m["db_id"] for m in members],
                "removable_from_state": any(m["langgraph_id"] for m in members),
            }
        )
    return {"thread_id": thread_id, "suggestions": out, "info": info}


class RelevanceApplyRequest(BaseModel):
    message_ids: List[int]
    episode_id: Optional[str] = None
    label: Optional[str] = None
    score: Optional[float] = None
    source: Optional[str] = None
    title: Optional[str] = None
    freed_tokens: Optional[int] = 0
    model: Optional[str] = None


@app.post("/sessions/{session_id}/threads/{thread_id}/relevance/apply")
def relevance_apply(
    session_id: str,
    thread_id: str,
    req: RelevanceApplyRequest,
    user_id: str = Depends(get_current_user),
):
    """Remove an accepted episode: delete its message rows + RemoveMessage them
    from the agent state, log a context_event, and record the choice as
    feedback for the training loop."""
    from relevance import record_feedback  # lazy

    removed = 0
    state_removed = 0
    lg_ids: List[str] = []
    with app.state.pool.connection() as conn:
        _verify_thread(conn, user_id, session_id, thread_id)
        for mid in req.message_ids:
            row = conn.execute(
                "SELECT langgraph_id FROM messages "
                "WHERE id = %s AND session_id = %s AND thread_id = %s AND user_id = %s",
                (mid, session_id, thread_id, user_id),
            ).fetchone()
            if not row:
                continue
            if row[0]:
                lg_ids.append(row[0])
            conn.execute("DELETE FROM messages WHERE id = %s", (mid,))
            removed += 1
        _record_context_event(
            conn,
            user_id,
            session_id,
            thread_id,
            turn_index=0,
            edit_type="relevance_pruning",
            freed_tokens=int(req.freed_tokens or 0),
            details={
                "episode_id": req.episode_id,
                "label": req.label,
                "source": req.source,
                "removed": removed,
                "title": (req.title or "")[:120],
            },
        )

    if lg_ids:
        try:
            agent = _get_agent(req.model)
            agent.update_state(
                {
                    "configurable": {
                        "thread_id": f"{user_id}:{session_id}",
                        "user_id": user_id,
                        "session_id": session_id,
                    }
                },
                {"messages": [RemoveMessage(id=i) for i in lg_ids]},
            )
            state_removed = len(lg_ids)
        except Exception as e:
            print(f"[relevance_apply] update_state failed: {e!r}", flush=True)

    try:
        record_feedback(
            {
                "surface": "website",
                "user_id": user_id,
                "session_id": session_id,
                "thread_id": thread_id,
                "episode_id": req.episode_id,
                "title": req.title,
                "shown_label": req.label,
                "user_action": "accept_drop",
                "final_label": req.label,
                "score": req.score,
                "source": req.source,
                "tokens": req.freed_tokens,
            }
        )
    except Exception as e:
        print(f"[relevance_apply] feedback log failed: {e!r}", flush=True)

    return {"ok": True, "removed": removed, "removed_from_state": state_removed}


class RelevanceSummarizeRequest(BaseModel):
    message_ids: List[int]
    episode_id: Optional[str] = None
    title: Optional[str] = None
    score: Optional[float] = None
    source: Optional[str] = None
    freed_tokens: Optional[int] = 0
    model: Optional[str] = None


@app.post("/sessions/{session_id}/threads/{thread_id}/relevance/summarize")
def relevance_summarize(
    session_id: str,
    thread_id: str,
    req: RelevanceSummarizeRequest,
    user_id: str = Depends(get_current_user),
):
    """Replace an episode's messages with a short LLM summary — saves tokens
    while keeping the gist (unlike Remove, which drops it entirely). Summarises
    the episode, deletes the originals (DB + checkpoint), and inserts one
    summary message in their place."""
    import uuid as _uuid

    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    # 1. Load the episode's messages in chronological order.
    with app.state.pool.connection() as conn:
        _verify_thread(conn, user_id, session_id, thread_id)
        rows = conn.execute(
            "SELECT id, role, content, tool_name, langgraph_id FROM messages "
            "WHERE id = ANY(%s) AND session_id = %s AND thread_id = %s "
            "AND user_id = %s ORDER BY id ASC",
            (list(req.message_ids), session_id, thread_id, user_id),
        ).fetchall()
    if not rows:
        raise HTTPException(404, "No messages found for this episode.")

    transcript = "\n".join(
        f"[{(tool_name or role or 'msg')}] {(content or '')[:2000]}"
        for _id, role, content, tool_name, _lg in rows
    )[:12000]

    # 2. Summarise via the active chat model.
    try:
        chat_model, _ = _resolve_chat_model(req.model, user_id, session_id)
        sys = (
            "Summarise this finished portion of an agent conversation in 2-4 "
            "sentences. Keep concrete results, decisions, file names and "
            "identifiers; drop verbose tool output. Output only the summary."
        )
        resp = chat_model.invoke(
            [SystemMessage(content=sys), HumanMessage(content=transcript)]
        )
        summary = getattr(resp, "content", "") or ""
        if isinstance(summary, list):
            summary = _tool_content_to_text(summary)
        summary = summary.strip()
    except Exception as e:
        raise HTTPException(500, f"Summarisation failed: {e}")
    if not summary:
        raise HTTPException(500, "Summariser returned empty text.")

    title = (req.title or "earlier step").strip()
    summary_text = f"[Summary of earlier step — {title}]\n{summary}"
    new_lg_id = f"acm-summary-{_uuid.uuid4().hex[:16]}"

    # 3. Delete originals from the DB + insert the summary row.
    lg_ids: List[str] = []
    removed = 0
    with app.state.pool.connection() as conn:
        for _id, role, content, tool_name, lg in rows:
            if lg:
                lg_ids.append(lg)
            conn.execute("DELETE FROM messages WHERE id = %s", (_id,))
            removed += 1
        _record_message(
            conn,
            session_id,
            thread_id,
            user_id,
            "assistant",
            summary_text,
            tokens=_estimate_tokens(summary_text),
            langgraph_id=new_lg_id,
        )
        _record_context_event(
            conn,
            user_id,
            session_id,
            thread_id,
            turn_index=0,
            edit_type="relevance_summarize",
            freed_tokens=int(req.freed_tokens or 0),
            details={
                "episode_id": req.episode_id,
                "title": title[:120],
                "removed": removed,
            },
        )

    # 4. Mirror to the checkpoint: drop originals, add the summary so the
    #    model actually sees fewer tokens next turn.
    state_removed = 0
    try:
        agent = _get_agent(req.model)
        cfg = {
            "configurable": {
                "thread_id": f"{user_id}:{session_id}",
                "user_id": user_id,
                "session_id": session_id,
            }
        }
        updates: List[Any] = [RemoveMessage(id=i) for i in lg_ids]
        updates.append(AIMessage(content=summary_text, id=new_lg_id))
        agent.update_state(cfg, {"messages": updates})
        state_removed = len(lg_ids)
    except Exception as e:
        print(f"[relevance_summarize] update_state failed: {e!r}", flush=True)

    try:
        from relevance import record_feedback  # lazy

        record_feedback(
            {
                "surface": "website",
                "user_id": user_id,
                "session_id": session_id,
                "thread_id": thread_id,
                "episode_id": req.episode_id,
                "title": req.title,
                "shown_label": "SUMMARIZE",
                "user_action": "accept_summarize",
                "final_label": "SUMMARIZE",
                "score": req.score,
                "source": req.source,
                "tokens": req.freed_tokens,
            }
        )
    except Exception as e:
        print(f"[relevance_summarize] feedback log failed: {e!r}", flush=True)

    return {
        "ok": True,
        "removed": removed,
        "removed_from_state": state_removed,
        "summary": summary_text,
    }


class RelevanceFeedbackRequest(BaseModel):
    episode_id: Optional[str] = None
    title: Optional[str] = None
    shown_label: Optional[str] = None
    user_action: Optional[str] = None
    final_label: Optional[str] = None
    score: Optional[float] = None
    source: Optional[str] = None
    tokens: Optional[int] = 0


@app.post("/sessions/{session_id}/threads/{thread_id}/relevance/feedback")
def relevance_feedback(
    session_id: str,
    thread_id: str,
    req: RelevanceFeedbackRequest,
    user_id: str = Depends(get_current_user),
):
    """Log a non-removal decision (reject/keep/edit) so the training loop sees
    the negative examples too, not just accepted drops."""
    from relevance import record_feedback  # lazy

    with app.state.pool.connection() as conn:
        _verify_thread(conn, user_id, session_id, thread_id)
    try:
        record_feedback(
            {
                "surface": "website",
                "user_id": user_id,
                "session_id": session_id,
                "thread_id": thread_id,
                **req.model_dump(),
            }
        )
    except Exception as e:
        raise HTTPException(500, f"feedback log failed: {e}")
    return {"ok": True}


@app.post("/chat")
def chat(req: ChatRequest, user_id: str = Depends(get_current_user)):
    import traceback

    # Clear any stale cancel flag from a previous turn — we're starting a
    # new one, so the user is committed to it running until they cancel
    # again.
    clear_cancel(req.thread_id)

    try:
        # Resolve which context-management profile applies to this turn.
        # Order: one-off body > saved id > legacy strategy > session
        # override > user default > built-in `minimal`.
        from context_profiles import resolve_profile  # lazy

        with app.state.pool.connection() as _conn:
            _profile, _profile_name = resolve_profile(
                _conn,
                user_id=user_id,
                session_id=req.session_id,
                request_profile_id=req.context_profile_id,
                request_profile_body=req.context_profile,
                legacy_strategy=req.context_strategy,
            )
        agent = _get_agent_for_request(
            req.model,
            user_id,
            session_id=req.session_id,
            profile=_profile,
        )
        _note_visual_method_skip(
            _profile, req.model, user_id, req.session_id, req.thread_id
        )

        # Project sessions get a sandboxed workspace; chat-only sessions don't.
        # The lazy-create returns an existing workspace if there is one (auto-
        # resumed if paused) — so the cost is amortised across turns. Failures
        # here are non-fatal: chat still works, only run_shell is unavailable.
        workspace_ref: Optional[str] = None
        workspace_id: Optional[str] = None
        session_mode = "auto"
        with app.state.pool.connection() as conn:
            session_row = conn.execute(
                "SELECT kind, mode FROM sessions WHERE id = %s AND user_id = %s",
                (req.session_id, user_id),
            ).fetchone()
        session_kind = session_row[0] if session_row else None
        if session_row and session_row[1]:
            session_mode = (session_row[1] or "auto").lower()
        if session_kind and (session_kind or "").lower() == "project":
            try:
                ws_row, workspace_ref = _ensure_workspace_for_session(user_id, req.session_id)
                workspace_id = ws_row["id"]
            except HTTPException as e:
                # Surface 429 (cap hit) to the user as a chat-level error so
                # they know why shell tools are unavailable; other backend
                # failures get logged and chat proceeds without a workspace.
                if e.status_code == 429:
                    raise
                print(f"[/chat] workspace provision failed: {e.detail}", flush=True)
            except Exception as e:
                print(f"[/chat] workspace provision crashed: {e!r}", flush=True)

        # Scope LangGraph thread by user+session to prevent cross-user collisions.
        # Inject user_id + session_id so file tools (list/read/write) can
        # resolve the current project's uploads dir without the model having
        # to pass them. workspace_ref reaches run_shell + future workspace
        # tools. Also attach a per-request logger so tool + LLM activity
        # prints to the backend terminal.
        configurable = {
            "thread_id": f"{user_id}:{req.session_id}",
            "user_id": user_id,
            "session_id": req.session_id,
            "session_mode": session_mode,
        }
        if workspace_ref:
            configurable["workspace_ref"] = workspace_ref
        config = {
            "callbacks": [
                AgentLogger(request_id=req.session_id),
                EventStreamer(thread_id=req.thread_id),
            ],
            "configurable": configurable,
        }

        # PR #3: context-editing pass. Runs every enabled technique on
        # the in-flight message list before agent.ainvoke. PR #3 ships
        # tool_result_trimming; PRs #5/#6 will add summarisation +
        # sliding window inside the same orchestrator.
        try:
            from context_editing import apply_context_edits  # lazy

            def _log_edit(edit_type, turn_index, freed_tokens, details):
                with app.state.pool.connection() as _ec:
                    _record_context_event(
                        _ec,
                        user_id=user_id,
                        session_id=req.session_id,
                        thread_id=req.thread_id,
                        turn_index=turn_index,
                        edit_type=edit_type,
                        freed_tokens=freed_tokens,
                        details=details,
                    )

            # Re-resolve the active chat model so the summariser step
            # (PR #5) has something to call. _resolve_chat_model is LRU-
            # cached, so the second call this turn is free.
            try:
                _chat_model, _ = _resolve_chat_model(
                    req.model, user_id, req.session_id
                )
            except Exception:
                _chat_model = None
            apply_context_edits(
                agent, config, _profile,
                chat_model=_chat_model,
                record_event=_log_edit,
                estimator=_estimate_tokens,
            )
            # Safety net: if the active model can't read images, drop any
            # image blocks left in the thread so a leftover visual-method
            # result can't 404 the turn ("no endpoints support image input").
            if _chat_model is not None and not _model_supports_vision(
                _model_id_of(_chat_model)
            ):
                from context_editing import sanitize_images_for_text_model

                n = sanitize_images_for_text_model(agent, config)
                if n:
                    print(
                        f"[/chat] stripped {n} image block(s) for text-only "
                        f"model {_model_id_of(_chat_model)!r}",
                        flush=True,
                    )
        except Exception as _e:
            # Edits are best-effort — never block the user's turn.
            print(f"[/chat] context_editing failed: {_e!r}", flush=True)

        # Record the user's message in DB *before* invoking the agent. That
        # way it survives in chat history even if the model crashes mid-turn.
        # We back-fill its langgraph_id after invoke so deletes can target the
        # message in LangGraph's checkpoint state too.
        user_row_id = 0
        with app.state.pool.connection() as conn:
            _verify_thread(conn, user_id, req.session_id, req.thread_id)
            user_row_id = _record_message(
                conn, req.session_id, req.thread_id, user_id, "user", req.message
            )

        try:
            pre_state = agent.get_state(config)
        except Exception as e:
            print(f"[/chat] get_state failed: {e!r}", flush=True)
            traceback.print_exc()
            _record_error_reply(
                req.session_id,
                req.thread_id,
                user_id,
                f"Checkpoint state error: {e}",
            )
            raise HTTPException(500, f"Checkpoint state error: {e}")
        pre_msgs = (pre_state.values or {}).get("messages", []) if pre_state else []
        pre_count = len(pre_msgs)

        # Build the actual prompt sent to the LLM. We deliberately do NOT
        # prepend a "ask first" instruction in confirm mode — the hard
        # LangGraph `interrupt()` inside write_project_file / run_shell is
        # the canonical gate. Otherwise the model would ask in chat ("Please
        # confirm…") AND the interrupt would fire, forcing the user to
        # approve twice.
        prefixes: list[str] = []
        if req.attached_files:
            file_list = ", ".join(req.attached_files)
            prefixes.append(
                f"[The user just attached the following files to this message: "
                f"{file_list}. Use read_project_file to read them before "
                f"answering.]"
            )

        # Apply skills for this turn: ones the user explicitly invoked via the
        # "/" menu, PLUS any enabled skills whose description keywords match the
        # message. The keyword match is a deterministic, server-side backstop:
        # smaller models (e.g. gemini-2.0-flash) don't reliably call the
        # read_skill tool, so we detect the relevant skill ourselves, inject its
        # real instructions, and record a "Skill: <name>" row so the timeline
        # always shows which skill was used — like a tool/sandbox step.
        _auto_skills: list[str] = []
        try:
            from skills_catalog import match_skills  # lazy

            _auto_skills = match_skills(
                req.message, _fetch_enabled_skill_rows(user_id)
            )
        except Exception as e:
            print(f"[skills] match failed: {e!r}", flush=True)
        _all_skills = list(dict.fromkeys(list(req.triggered_skills) + _auto_skills))
        if _all_skills:
            _applied = _apply_triggered_skills(
                user_id, req.session_id, req.thread_id, _all_skills
            )
            if _applied:
                prefixes.append(_applied)

        llm_input = (
            "\n\n".join(prefixes + [req.message]) if prefixes else req.message
        )

        try:
            # ainvoke + asyncio.run: MCP tools from langchain-mcp-adapters
            # are async-only (coroutine-backed StructuredTool), so the
            # sync invoke path raises "StructuredTool does not support
            # sync invocation" the moment the agent tries to call one.
            # FastAPI runs this sync handler in a threadpool worker with
            # no loop attached, so asyncio.run is safe.
            result = asyncio.run(
                agent.ainvoke(
                    {"messages": [HumanMessage(content=llm_input)]},
                    config=config,
                )
            )
        except Exception as e:
            inner = _unwrap_exc(e)
            msg = str(inner) or inner.__class__.__name__
            cls = inner.__class__.__name__
            print(
                f"[/chat] model invoke failed ({cls} via {e.__class__.__name__}): {msg}",
                flush=True,
            )
            traceback.print_exc()
            low = msg.lower()
            low_cls = cls.lower()
            # Match both the string body and the exception class name so that
            # wrapped errors (LangGraph/LangChain sometimes re-raise as
            # different types) still get classified as rate-limit.
            if (
                "429" in msg
                or "rate" in low
                or "quota" in low
                or "ratelimit" in low_cls
                or "toomanyrequest" in low_cls
            ):
                _record_error_reply(
                    req.session_id,
                    req.thread_id,
                    user_id,
                    "Model rate-limited. Wait a minute and retry, or add OpenRouter credit.",
                )
                raise HTTPException(429, "Model rate-limited. Wait a minute and retry, or add OpenRouter credit.")
            if "401" in msg or "unauthorized" in low or "api key" in low:
                _record_error_reply(
                    req.session_id,
                    req.thread_id,
                    user_id,
                    f"Model auth failed (check OPENROUTER_API_KEY): {msg[:200]}",
                )
                raise HTTPException(401, f"Model auth failed (check OPENROUTER_API_KEY): {msg[:200]}")
            _record_error_reply(
                req.session_id, req.thread_id, user_id, f"Model error: {msg[:300]}"
            )
            raise HTTPException(500, f"Model error: {msg[:300]}")

        reply = ""
        new_messages = result["messages"][pre_count:]
        try:
            with app.state.pool.connection() as conn:
                for msg in new_messages:
                    msg_id = getattr(msg, "id", None)
                    if isinstance(msg, HumanMessage):
                        # The user message was recorded pre-invoke. Back-fill
                        # its langgraph_id now so deletes can also remove it
                        # from LangGraph's checkpoint state.
                        if msg_id and user_row_id:
                            conn.execute(
                                "UPDATE messages SET langgraph_id = %s WHERE id = %s",
                                (msg_id, user_row_id),
                            )
                        continue
                    elif isinstance(msg, AIMessage):
                        tool_calls = []
                        if msg.tool_calls:
                            for tc in msg.tool_calls:
                                tool_calls.append({"name": tc["name"], "args": tc["args"]})
                        content = msg.content or ""
                        if content or tool_calls:
                            breakdown = _ai_token_breakdown(msg)
                            _record_message(
                                conn,
                                req.session_id,
                                req.thread_id,
                                user_id,
                                "assistant",
                                content,
                                tool_calls=tool_calls or None,
                                tokens=breakdown["total"],
                                input_tokens=breakdown["input"],
                                output_tokens=breakdown["output"],
                                thinking_tokens=breakdown["thinking"],
                                cache_read_tokens=breakdown.get("cache_read", 0),
                                langgraph_id=msg_id,
                            )
                        if content:
                            reply = content
                    elif isinstance(msg, ToolMessage):
                        _record_message(
                            conn,
                            req.session_id,
                            req.thread_id,
                            user_id,
                            "tool",
                            _tool_content_to_text(msg.content),
                            tool_name=getattr(msg, "name", ""),
                            langgraph_id=msg_id,
                        )
        except Exception as e:
            print(f"[/chat] DB write failed: {e!r}", flush=True)
            traceback.print_exc()
            raise HTTPException(500, f"DB error while recording messages: {e}")

        # Mirror any new git commits into workspace_commits so the History UI
        # and undo endpoint can see them. Best-effort — failures don't break
        # the reply.
        if workspace_id and workspace_ref:
            try:
                _sync_workspace_commits(
                    user_id, req.session_id, workspace_id, workspace_ref
                )
            except Exception as e:
                print(f"[/chat] commit-sync failed: {e!r}", flush=True)

        # Detect a pending interrupt (Confirm-mode approval gate). If found,
        # surface to the UI so it can render an approval card; the user's
        # decision comes back via POST /resume.
        pending = _pending_approval(agent, config)
        if pending is not None:
            try:
                event_bus.publish(
                    f"thread:{req.thread_id}",
                    {"type": "approval_request", **pending},
                )
            except Exception as e:
                print(f"[/chat] approval_request publish failed: {e!r}", flush=True)
            if workspace_id and workspace_ref:
                try:
                    _sync_workspace_commits(
                        user_id, req.session_id, workspace_id, workspace_ref
                    )
                except Exception:
                    pass
            return {
                "reply": reply,
                "interrupted": True,
                "approval": pending,
            }

        # Mirror any new git commits into workspace_commits so the History UI
        # and undo endpoint can see them. Best-effort — failures don't break
        # the reply.
        if workspace_id and workspace_ref:
            try:
                _sync_workspace_commits(
                    user_id, req.session_id, workspace_id, workspace_ref
                )
            except Exception as e:
                print(f"[/chat] commit-sync failed: {e!r}", flush=True)

        return {"reply": reply, "interrupted": False}

    except HTTPException:
        raise
    except Exception as e:
        # Catch-all so the frontend gets a real message instead of "Internal Server Error".
        print(f"[/chat] UNHANDLED: {e!r}", flush=True)
        traceback.print_exc()
        # Persist the error to the chat history so the user sees WHY this turn
        # failed instead of just an opaque toast and no trace. Without this,
        # failures after agent.invoke (recording loop, commit sync, etc.) show
        # only a generic 500 with no chat-side breadcrumb.
        _record_error_reply(
            req.session_id,
            req.thread_id,
            user_id,
            f"{e.__class__.__name__}: {str(e)[:300]}",
        )
        raise HTTPException(500, f"Server error: {e.__class__.__name__}: {str(e)[:300]}")


def _pending_approval(agent, config) -> Optional[dict]:
    """Return the first pending interrupt's value, or None if the graph is done.

    LangGraph stores interrupts on `state.tasks[*].interrupts`. We surface
    only the most recent one — a single user decision releases it, and the
    graph then runs to either completion or the next interrupt, at which
    point this is called again.
    """
    try:
        state = agent.get_state(config)
    except Exception as e:
        print(f"[interrupt-check] get_state failed: {e!r}", flush=True)
        return None
    if not getattr(state, "next", None):
        return None
    for task in getattr(state, "tasks", []) or []:
        for it in getattr(task, "interrupts", None) or []:
            value = getattr(it, "value", None)
            if isinstance(value, dict):
                return value
    return None


class ResumeChatRequest(BaseModel):
    session_id: str
    thread_id: str
    approved: bool
    reason: Optional[str] = None
    model: Optional[str] = None
    context_strategy: Optional[str] = None
    context_profile_id: Optional[str] = None
    context_profile: Optional[Dict[str, Any]] = None


class CancelChatRequest(BaseModel):
    session_id: str
    thread_id: str


@app.post("/chat/cancel")
def cancel_chat(req: CancelChatRequest, user_id: str = Depends(get_current_user)):
    """Interrupt an in-flight agent turn.

    Two-pronged cancel:
      1. Flip `cancel_registry` for this thread — every tool call after this
         point short-circuits and returns "Cancelled by user". Any currently-
         running shell command is also killed via `pkill`. The agent loop
         observes the cancellation through tool results.
      2. Publish an SSE `cancelled` event so the UI can drop its in-flight
         rows / thinking indicator immediately.

    We still can't cancel the upstream LLM call mid-flight — if the agent
    is purely thinking when cancel fires, that single token stream finishes,
    but every subsequent tool call returns the cancelled marker so the loop
    unwinds at the next step.
    """
    # Flag first, kill processes second — tools that haven't started yet
    # see the flag at entry; tools that ARE running get killed via pkill.
    request_cancel(req.thread_id)

    with app.state.pool.connection() as conn:
        _verify_thread(conn, user_id, req.session_id, req.thread_id)
        # Find a live workspace for this session, if any.
        row = conn.execute(
            """
            SELECT backend_ref FROM workspaces
            WHERE session_id = %s AND user_id = %s AND status != 'destroyed'
            ORDER BY created_at DESC LIMIT 1
            """,
            (req.session_id, user_id),
        ).fetchone()
    killed = False
    if row:
        backend_ref = row[0]
        try:
            # Kill all children of pid 1 (the `sleep infinity` keeping the
            # container alive). Anything the agent spawned is a child of an
            # exec call which itself parents from pid 1, so this catches both.
            get_backend().exec(
                backend_ref,
                "pkill -KILL -P 1 2>/dev/null || true",
                timeout=5,
            )
            killed = True
        except SandboxError as e:
            print(f"[/chat/cancel] kill exec failed: {e}", flush=True)

    try:
        event_bus.publish(
            f"thread:{req.thread_id}",
            {"type": "cancelled", "killed_processes": killed},
        )
    except Exception:
        pass
    return {"ok": True, "killed": killed}


@app.post("/chat/resume")
def chat_resume(req: ResumeChatRequest, user_id: str = Depends(get_current_user)):
    """Resume a chat turn that paused on a Confirm-mode interrupt.

    Mirrors /chat but feeds the user's approval decision into LangGraph's
    `interrupt()` return value via `Command(resume=...)`. After resume, the
    graph runs until the next interrupt or completion — same loop semantics
    as the original /chat path.
    """
    import traceback

    clear_cancel(req.thread_id)

    try:
        # Resolve which context-management profile applies to this turn.
        # Order: one-off body > saved id > legacy strategy > session
        # override > user default > built-in `minimal`.
        from context_profiles import resolve_profile  # lazy

        with app.state.pool.connection() as _conn:
            _profile, _profile_name = resolve_profile(
                _conn,
                user_id=user_id,
                session_id=req.session_id,
                request_profile_id=req.context_profile_id,
                request_profile_body=req.context_profile,
                legacy_strategy=req.context_strategy,
            )
        agent = _get_agent_for_request(
            req.model,
            user_id,
            session_id=req.session_id,
            profile=_profile,
        )
        _note_visual_method_skip(
            _profile, req.model, user_id, req.session_id, req.thread_id
        )

        # Recover session mode + workspace state to rebuild the same config.
        workspace_ref: Optional[str] = None
        workspace_id: Optional[str] = None
        session_mode = "auto"
        with app.state.pool.connection() as conn:
            _verify_thread(conn, user_id, req.session_id, req.thread_id)
            row = conn.execute(
                "SELECT kind, mode FROM sessions WHERE id = %s AND user_id = %s",
                (req.session_id, user_id),
            ).fetchone()
        if row:
            session_mode = (row[1] or "auto").lower()
            if (row[0] or "").lower() == "project":
                try:
                    ws_row, workspace_ref = _ensure_workspace_for_session(
                        user_id, req.session_id
                    )
                    workspace_id = ws_row["id"]
                except HTTPException as e:
                    if e.status_code == 429:
                        raise
                    print(
                        f"[/chat/resume] workspace lookup failed: {e.detail}",
                        flush=True,
                    )

        configurable = {
            "thread_id": f"{user_id}:{req.session_id}",
            "user_id": user_id,
            "session_id": req.session_id,
            "session_mode": session_mode,
        }
        if workspace_ref:
            configurable["workspace_ref"] = workspace_ref
        config = {
            "callbacks": [
                AgentLogger(request_id=req.session_id),
                EventStreamer(thread_id=req.thread_id),
            ],
            "configurable": configurable,
        }

        # PR #3: same context-editing pass as /chat. The user might have
        # spent several turns under this conversation since the last
        # resume, so a long-running approval flow still benefits.
        try:
            from context_editing import apply_context_edits  # lazy

            def _log_edit(edit_type, turn_index, freed_tokens, details):
                with app.state.pool.connection() as _ec:
                    _record_context_event(
                        _ec,
                        user_id=user_id,
                        session_id=req.session_id,
                        thread_id=req.thread_id,
                        turn_index=turn_index,
                        edit_type=edit_type,
                        freed_tokens=freed_tokens,
                        details=details,
                    )

            # Re-resolve the active chat model so the summariser step
            # (PR #5) has something to call. _resolve_chat_model is LRU-
            # cached, so the second call this turn is free.
            try:
                _chat_model, _ = _resolve_chat_model(
                    req.model, user_id, req.session_id
                )
            except Exception:
                _chat_model = None
            apply_context_edits(
                agent, config, _profile,
                chat_model=_chat_model,
                record_event=_log_edit,
                estimator=_estimate_tokens,
            )
            # Safety net: strip leftover image blocks for a text-only model
            # (see /chat for the rationale).
            if _chat_model is not None and not _model_supports_vision(
                _model_id_of(_chat_model)
            ):
                from context_editing import sanitize_images_for_text_model

                sanitize_images_for_text_model(agent, config)
        except Exception as _e:
            print(f"[/chat/resume] context_editing failed: {_e!r}", flush=True)

        pre_state = agent.get_state(config)
        pre_count = len(
            (pre_state.values or {}).get("messages", []) if pre_state else []
        )

        try:
            # See /chat for why we go through ainvoke + asyncio.run when
            # async-only MCP tools are in the toolbox.
            result = asyncio.run(
                agent.ainvoke(
                    Command(resume={"approved": req.approved, "reason": req.reason}),
                    config=config,
                )
            )
        except Exception as e:
            inner = _unwrap_exc(e)
            msg = str(inner) or inner.__class__.__name__
            print(
                f"[/chat/resume] model invoke failed ({inner.__class__.__name__} via {e.__class__.__name__}): {msg}",
                flush=True,
            )
            traceback.print_exc()
            raise HTTPException(500, f"Resume failed: {msg[:300]}")

        # Persist new messages produced after the resume (same logic as /chat).
        reply = ""
        new_messages = result["messages"][pre_count:]
        try:
            with app.state.pool.connection() as conn:
                for msg in new_messages:
                    if isinstance(msg, HumanMessage):
                        continue
                    if isinstance(msg, AIMessage):
                        tool_calls = []
                        for tc in msg.tool_calls or []:
                            tool_calls.append({"name": tc["name"], "args": tc["args"]})
                        content = msg.content or ""
                        if content or tool_calls:
                            _bd = _ai_token_breakdown(msg)
                            _record_message(
                                conn,
                                req.session_id,
                                req.thread_id,
                                user_id,
                                "assistant",
                                content,
                                tool_calls=tool_calls or None,
                                tokens=_bd.get("total", 0),
                                input_tokens=_bd.get("input", 0),
                                output_tokens=_bd.get("output", 0),
                                thinking_tokens=_bd.get("thinking", 0),
                                cache_read_tokens=_bd.get("cache_read", 0),
                                langgraph_id=getattr(msg, "id", None),
                            )
                        if content:
                            reply = content
                    elif isinstance(msg, ToolMessage):
                        _record_message(
                            conn,
                            req.session_id,
                            req.thread_id,
                            user_id,
                            "tool",
                            _tool_content_to_text(msg.content),
                            tool_name=getattr(msg, "name", ""),
                            langgraph_id=getattr(msg, "id", None),
                        )
        except Exception as e:
            print(f"[/chat/resume] DB write failed: {e!r}", flush=True)

        pending = _pending_approval(agent, config)
        if pending is not None:
            try:
                event_bus.publish(
                    f"thread:{req.thread_id}",
                    {"type": "approval_request", **pending},
                )
            except Exception:
                pass
            if workspace_id and workspace_ref:
                try:
                    _sync_workspace_commits(
                        user_id, req.session_id, workspace_id, workspace_ref
                    )
                except Exception:
                    pass
            return {"reply": reply, "interrupted": True, "approval": pending}

        if workspace_id and workspace_ref:
            try:
                _sync_workspace_commits(
                    user_id, req.session_id, workspace_id, workspace_ref
                )
            except Exception:
                pass
        return {"reply": reply, "interrupted": False}

    except HTTPException:
        raise
    except Exception as e:
        print(f"[/chat/resume] UNHANDLED: {e!r}", flush=True)
        traceback.print_exc()
        raise HTTPException(
            500, f"Server error: {e.__class__.__name__}: {str(e)[:300]}"
        )
