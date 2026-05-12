import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from jwt import PyJWKClient
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from Tools import all_tools
from agent_callbacks import AgentLogger
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

model = ChatOpenAI(
    # Note: glm-4.5-air:free has a known bug where it wraps multi-arg tool
    # call `args` in a list, breaking AIMessage validation. Llama-3.3 + Qwen-2.5
    # handle structured tool calls correctly. Override via CHAT_MODEL env var.
    model=os.getenv("CHAT_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
    openai_api_key=os.getenv("OPENROUTER_API_KEY"),
    openai_api_base="https://openrouter.ai/api/v1",
    max_tokens=int(os.getenv("CHAT_MAX_TOKENS", "1500")),
    temperature=float(os.getenv("CHAT_TEMPERATURE", "0.3")),
    default_headers={
        "HTTP-Referer": "http://localhost",
        "X-Title": "FYP Agent Project",
    },
)


class CreateSessionRequest(BaseModel):
    name: str
    kind: Optional[str] = "chat"  # "project" auto-creates starter files


class CreateThreadRequest(BaseModel):
    name: str


class ChatRequest(BaseModel):
    session_id: str
    thread_id: str
    message: str
    attached_files: List[str] = []


class TitleRequest(BaseModel):
    text: str


class GithubTokenRequest(BaseModel):
    token: str


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
    global _saver_cm

    pool = ConnectionPool(DB_URL, min_size=1, max_size=10, kwargs={"autocommit": True})
    pool.wait()

    _saver_cm = PostgresSaver.from_conn_string(DB_URL)
    saver = _saver_cm.__enter__()
    saver.setup()

    agent = create_agent(
        model=model,
        tools=all_tools,
        system_prompt=SYSTEM_PROMPT,
        checkpointer=saver,
    )

    app.state.agent = agent
    app.state.pool = pool

    try:
        yield
    finally:
        _saver_cm.__exit__(None, None, None)
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
):
    conn.execute(
        """
        INSERT INTO messages
            (session_id, thread_id, user_id, role, content, tool_name, tool_calls_json, tokens)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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
        ),
    )


def _ai_tokens(msg) -> int:
    """Best-effort token total for an AIMessage."""
    um = getattr(msg, "usage_metadata", None)
    if um:
        try:
            total = um["total_tokens"] if hasattr(um, "__getitem__") else getattr(um, "total_tokens", 0)
            if total:
                return int(total)
        except Exception:
            pass
    rm = getattr(msg, "response_metadata", None) or {}
    tu = rm.get("token_usage") or rm.get("usage") or {}
    try:
        return int(tu.get("total_tokens", 0) or 0)
    except Exception:
        return 0


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
            SELECT s.id, s.name, s.created_at,
                   COALESCE(SUM(m.tokens), 0)::int AS tokens
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            WHERE s.user_id = %s
            GROUP BY s.id, s.name, s.created_at
            ORDER BY s.created_at DESC
            """,
            (user_id,),
        ).fetchall()
    return [
        {
            "id": str(r[0]),
            "name": r[1],
            "created_at": r[2].isoformat(),
            "tokens": int(r[3] or 0),
        }
        for r in rows
    ]


@app.post("/sessions")
def create_session(req: CreateSessionRequest, user_id: str = Depends(get_current_user)):
    with app.state.pool.connection() as conn:
        s = conn.execute(
            "INSERT INTO sessions (user_id, name) VALUES (%s, %s) "
            "RETURNING id, name, created_at",
            (user_id, req.name),
        ).fetchone()
        sid, sname, screated = s
        t = conn.execute(
            "INSERT INTO threads (session_id, user_id, name) VALUES (%s, %s, %s) "
            "RETURNING id, name, created_at",
            (sid, user_id, "General"),
        ).fetchone()

    # For projects, seed two starter files the agent maintains over time.
    if (req.kind or "").lower() == "project":
        _seed_project_files(user_id, str(sid), sname)

    return {
        "id": str(sid),
        "name": sname,
        "created_at": screated.isoformat(),
        "tokens": 0,
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


@app.get("/sessions/{session_id}/files")
def list_files(
    session_id: str,
    user_id: str = Depends(get_current_user),
):
    with app.state.pool.connection() as conn:
        _verify_session(conn, user_id, session_id)

    bucket = get_bucket()
    try:
        items = bucket.list(session_prefix(user_id, session_id))
    except Exception as e:
        raise HTTPException(500, f"List failed: {e}")
    out = []
    for it in items or []:
        # Storage returns a placeholder row with id=None for empty folders.
        if not it.get("id"):
            continue
        meta = it.get("metadata") or {}
        out.append(
            {
                "name": it.get("name"),
                "size": meta.get("size", 0),
                "modified_at": it.get("updated_at") or it.get("created_at"),
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
    bucket = get_bucket()
    try:
        bucket.remove([file_key(user_id, session_id, name)])
    except Exception as e:
        if not is_not_found(e):
            raise HTTPException(500, f"Delete failed: {e}")
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
    bucket = get_bucket()
    try:
        data: bytes = bucket.download(file_key(user_id, session_id, name))
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
        result = model.invoke([HumanMessage(content=prompt)])
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
            SELECT role, content, tool_name, tool_calls_json
            FROM messages
            WHERE session_id = %s AND thread_id = %s AND user_id = %s
            ORDER BY id ASC
            """,
            (session_id, thread_id, user_id),
        ).fetchall()
    out = []
    for role, content, tool_name, tool_calls_json in rows:
        m: dict = {"role": role, "content": content}
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


@app.get("/sessions/{session_id}/threads/{thread_id}/context")
def get_context(
    session_id: str,
    thread_id: str,
    user_id: str = Depends(get_current_user),
):
    """Return what the LLM sees on the next turn: system prompt, message
    history, attached files, and approximate token usage."""
    with app.state.pool.connection() as conn:
        _verify_thread(conn, user_id, session_id, thread_id)
        rows = conn.execute(
            """
            SELECT role, content, tool_name, tool_calls_json, tokens
            FROM messages
            WHERE session_id = %s AND thread_id = %s AND user_id = %s
            ORDER BY id ASC
            """,
            (session_id, thread_id, user_id),
        ).fetchall()

    messages = []
    total_tokens = 0
    for role, content, tool_name, tool_calls_json, recorded_tokens in rows:
        # Prefer the recorded token count for assistant messages (real usage
        # from the API). Fall back to a rough char-based estimate otherwise.
        if recorded_tokens and recorded_tokens > 0:
            tokens = int(recorded_tokens)
        else:
            tokens = _estimate_tokens(content or "")
        total_tokens += tokens
        m = {
            "role": role,
            "content": content,
            "tokens": tokens,
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

    model_name = os.getenv("CHAT_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
    context_limit = _MODEL_CONTEXT_LIMITS.get(model_name, 128000)

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


@app.post("/chat")
def chat(req: ChatRequest, user_id: str = Depends(get_current_user)):
    import traceback

    try:
        agent = app.state.agent
        # Scope LangGraph thread by user+session to prevent cross-user collisions.
        # Inject user_id + session_id so file tools (list/read/write) can
        # resolve the current project's uploads dir without the model having
        # to pass them. Also attach a per-request logger so tool + LLM
        # activity prints to the backend terminal.
        config = {
            "callbacks": [AgentLogger(request_id=req.session_id)],
            "configurable": {
                "thread_id": f"{user_id}:{req.session_id}",
                "user_id": user_id,
                "session_id": req.session_id,
            },
        }

        # Record the user's message in DB *before* invoking the agent. That
        # way it survives in chat history even if the model crashes mid-turn.
        with app.state.pool.connection() as conn:
            _verify_thread(conn, user_id, req.session_id, req.thread_id)
            _record_message(
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

        # Build the actual prompt sent to the LLM. If the user just attached
        # files, prepend a hint so the agent knows to read them with the
        # read_project_file tool instead of asking what to explain.
        if req.attached_files:
            file_list = ", ".join(req.attached_files)
            llm_input = (
                f"[The user just attached the following files to this message: "
                f"{file_list}. Use read_project_file to read them before "
                f"answering.]\n\n{req.message}"
            )
        else:
            llm_input = req.message

        try:
            result = agent.invoke(
                {"messages": [HumanMessage(content=llm_input)]},
                config=config,
            )
        except Exception as e:
            msg = str(e) or e.__class__.__name__
            print(f"[/chat] model invoke failed: {msg}", flush=True)
            traceback.print_exc()
            low = msg.lower()
            if "429" in msg or "rate" in low or "quota" in low:
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
                    if isinstance(msg, HumanMessage):
                        # Already recorded before agent.invoke — skip to avoid
                        # duplicating the user's message in the chat history.
                        continue
                    elif isinstance(msg, AIMessage):
                        tool_calls = []
                        if msg.tool_calls:
                            for tc in msg.tool_calls:
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
                                tokens=_ai_tokens(msg),
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
                        )
        except Exception as e:
            print(f"[/chat] DB write failed: {e!r}", flush=True)
            traceback.print_exc()
            raise HTTPException(500, f"DB error while recording messages: {e}")

        return {"reply": reply}

    except HTTPException:
        raise
    except Exception as e:
        # Catch-all so the frontend gets a real message instead of "Internal Server Error".
        print(f"[/chat] UNHANDLED: {e!r}", flush=True)
        traceback.print_exc()
        raise HTTPException(500, f"Server error: {e.__class__.__name__}: {str(e)[:300]}")
