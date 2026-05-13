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
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres import PostgresSaver
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

DEFAULT_MODEL = os.getenv("CHAT_MODEL", "meta-llama/llama-3.3-70b-instruct:free")


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


def _get_agent(model_name: Optional[str]):
    name = (model_name or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    with _agent_cache_lock:
        if name in _agent_cache:
            return _agent_cache[name]
        agent = create_agent(
            model=_build_model(name),
            tools=all_tools,
            system_prompt=SYSTEM_PROMPT,
            checkpointer=app.state.saver,
        )
        _agent_cache[name] = agent
        return agent


# ── OpenRouter model catalog (cached) ───────────────────────────────────────
_models_cache: Dict[str, Any] = {"at": 0.0, "items": []}
_models_lock = Lock()
_MODELS_TTL_SECONDS = 600  # 10 minutes


def _fetch_free_models() -> List[Dict[str, Any]]:
    """Pull the OpenRouter catalog and keep only `:free` models. Cached."""
    now = time.time()
    with _models_lock:
        if _models_cache["items"] and (now - _models_cache["at"]) < _MODELS_TTL_SECONDS:
            return _models_cache["items"]
    try:
        r = requests.get("https://openrouter.ai/api/v1/models", timeout=10)
        r.raise_for_status()
        data = r.json().get("data", [])
    except Exception as e:
        print(f"[/models] OpenRouter fetch failed: {e!r}", flush=True)
        # Fall back to whatever we cached previously (possibly stale, possibly empty).
        return _models_cache["items"]
    items = []
    for m in data:
        mid = m.get("id") or ""
        if not mid.endswith(":free"):
            continue
        items.append({
            "id": mid,
            "name": m.get("name") or mid,
            "context_length": m.get("context_length") or 0,
            "description": (m.get("description") or "")[:200],
        })
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

    saver = PostgresSaver(pool)
    saver.setup()

    # Idempotent migrations for fields added after db/init.sql first shipped.
    with pool.connection() as conn:
        conn.execute(
            "ALTER TABLE messages "
            "ADD COLUMN IF NOT EXISTS input_tokens    integer NOT NULL DEFAULT 0, "
            "ADD COLUMN IF NOT EXISTS output_tokens   integer NOT NULL DEFAULT 0, "
            "ADD COLUMN IF NOT EXISTS thinking_tokens integer NOT NULL DEFAULT 0, "
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

    app.state.saver = saver
    app.state.pool = pool

    # Pre-build the agent for the default model so the first /chat request
    # doesn't pay the create_agent cost. Other models are built lazily by
    # _get_agent on first use.
    _get_agent(DEFAULT_MODEL)

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
    langgraph_id: Optional[str] = None,
) -> int:
    row = conn.execute(
        """
        INSERT INTO messages
            (session_id, thread_id, user_id, role, content, tool_name,
             tool_calls_json, tokens, input_tokens, output_tokens,
             thinking_tokens, langgraph_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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

    return {"total": total, "input": input_, "output": output, "thinking": thinking}


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
                   COALESCE(SUM(m.tokens), 0)::int AS tokens
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            WHERE s.user_id = %s
            GROUP BY s.id, s.name, s.kind, s.mode, s.created_at,
                     s.github_owner, s.github_repo, s.github_branch
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
            "tokens": int(r[8] or 0),
        }
        for r in rows
    ]


class UpdateSessionRequest(BaseModel):
    mode: Optional[str] = None  # 'auto' | 'confirm'


@app.patch("/sessions/{session_id}")
def update_session(
    session_id: str,
    req: UpdateSessionRequest,
    user_id: str = Depends(get_current_user),
):
    """Partial-update a session. Currently supports `mode` only."""
    if req.mode is not None and req.mode not in {"auto", "confirm"}:
        raise HTTPException(400, "mode must be 'auto' or 'confirm'")

    with app.state.pool.connection() as conn:
        _verify_session(conn, user_id, session_id)
        if req.mode is not None:
            conn.execute(
                "UPDATE sessions SET mode = %s WHERE id = %s AND user_id = %s",
                (req.mode, session_id, user_id),
            )
    return {"ok": True, "mode": req.mode}


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


# Context window sizes for common models (in tokens). Used to compute %used.
_MODEL_CONTEXT_LIMITS = {
    "meta-llama/llama-3.3-70b-instruct:free": 131072,
    "z-ai/glm-4.5-air:free": 131072,
    "qwen/qwen-2.5-72b-instruct:free": 131072,
    "google/gemini-2.0-flash-exp:free": 1048576,
    "openai/gpt-4o-mini": 128000,
    "anthropic/claude-haiku-4-5": 200000,
}


@app.get("/models")
def list_models(user_id: str = Depends(get_current_user)):
    """Return the list of OpenRouter `:free` models the UI can pick from.
    Cached server-side for 10 minutes."""
    items = _fetch_free_models()
    return {"default": DEFAULT_MODEL, "models": items}


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
                   langgraph_id
            FROM messages
            WHERE session_id = %s AND thread_id = %s AND user_id = %s
            ORDER BY id ASC
            """,
            (session_id, thread_id, user_id),
        ).fetchall()

    messages = []
    total_tokens = 0
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
        ) = row
        if recorded_tokens and recorded_tokens > 0:
            tokens = int(recorded_tokens)
        else:
            tokens = _estimate_tokens(content or "")
        total_tokens += tokens
        m = {
            "id": int(row_id),
            "role": role,
            "content": content,
            "tokens": tokens,
            "input_tokens": int(input_tok or 0),
            "output_tokens": int(output_tok or 0),
            "thinking_tokens": int(thinking_tok or 0),
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
    total_tokens += sys_tokens

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
        "messages": messages,
        "files": files,
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


@app.post("/chat")
def chat(req: ChatRequest, user_id: str = Depends(get_current_user)):
    import traceback

    # Clear any stale cancel flag from a previous turn — we're starting a
    # new one, so the user is committed to it running until they cancel
    # again.
    clear_cancel(req.thread_id)

    try:
        agent = _get_agent(req.model)

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
        llm_input = (
            "\n\n".join(prefixes + [req.message]) if prefixes else req.message
        )

        try:
            result = agent.invoke(
                {"messages": [HumanMessage(content=llm_input)]},
                config=config,
            )
        except Exception as e:
            msg = str(e) or e.__class__.__name__
            cls = e.__class__.__name__
            print(f"[/chat] model invoke failed ({cls}): {msg}", flush=True)
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
                            str(msg.content),
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
        agent = _get_agent(req.model)

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

        pre_state = agent.get_state(config)
        pre_count = len(
            (pre_state.values or {}).get("messages", []) if pre_state else []
        )

        try:
            result = agent.invoke(
                Command(resume={"approved": req.approved, "reason": req.reason}),
                config=config,
            )
        except Exception as e:
            msg = str(e) or e.__class__.__name__
            print(f"[/chat/resume] model invoke failed: {msg}", flush=True)
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
                            _record_message(
                                conn,
                                req.session_id,
                                req.thread_id,
                                user_id,
                                "assistant",
                                content,
                                tool_calls=tool_calls or None,
                                tokens=_ai_token_breakdown(msg).get("total", 0),
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
                            str(msg.content),
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
