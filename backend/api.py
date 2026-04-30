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

load_dotenv()

DB_URL = os.environ["SUPABASE_DB_URL"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
JWKS_URL = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"
_jwks_client = PyJWKClient(JWKS_URL)

# ── File uploads ────────────────────────────────────────────────────────────
UPLOADS_DIR = Path(os.environ.get("UPLOADS_DIR", "uploads")).resolve()
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB per file


def _session_dir(user_id: str, session_id: str) -> Path:
    d = (UPLOADS_DIR / user_id / session_id).resolve()
    # Belt-and-suspenders: refuse if user_id/session_id smuggled traversal in.
    if UPLOADS_DIR not in d.parents and d != UPLOADS_DIR:
        raise HTTPException(400, "Invalid session path")
    d.mkdir(parents=True, exist_ok=True)
    return d


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
    model="z-ai/glm-4.5-air:free",
    openai_api_key=os.getenv("OPENROUTER_API_KEY"),
    openai_api_base="https://openrouter.ai/api/v1",
    max_tokens=1000,
    temperature=0.5,
    default_headers={
        "HTTP-Referer": "http://localhost",
        "X-Title": "FYP Agent Project",
    },
)


class CreateSessionRequest(BaseModel):
    name: str


class CreateThreadRequest(BaseModel):
    name: str


class ChatRequest(BaseModel):
    session_id: str
    thread_id: str
    message: str


class TitleRequest(BaseModel):
    text: str


_saver_cm = None


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
        system_prompt=(
            "You are a helpful assistant. Use tools when needed. "
            "Remember everything the user tells you across this project/session."
        ),
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


@app.post("/sessions/{session_id}/files")
async def upload_files(
    session_id: str,
    files: List[UploadFile] = File(...),
    user_id: str = Depends(get_current_user),
):
    with app.state.pool.connection() as conn:
        _verify_session(conn, user_id, session_id)

    sdir = _session_dir(user_id, session_id)
    saved = []
    for f in files:
        name = _safe_filename(f.filename or "unnamed")
        target = sdir / name
        size = 0
        with target.open("wb") as out:
            while True:
                chunk = await f.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    out.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(
                        413,
                        f"{name} exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit",
                    )
                out.write(chunk)
        saved.append({"name": name, "size": size})
    return {"saved": saved}


@app.get("/sessions/{session_id}/files")
def list_files(
    session_id: str,
    user_id: str = Depends(get_current_user),
):
    with app.state.pool.connection() as conn:
        _verify_session(conn, user_id, session_id)

    sdir = _session_dir(user_id, session_id)
    out = []
    for p in sorted(sdir.iterdir()):
        if p.is_file():
            stat = p.stat()
            out.append(
                {
                    "name": p.name,
                    "size": stat.st_size,
                    "modified_at": stat.st_mtime,
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
    sdir = _session_dir(user_id, session_id)
    target = (sdir / name).resolve()
    if sdir not in target.parents:
        raise HTTPException(400, "Invalid path")
    if target.exists():
        target.unlink()
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
    sdir = _session_dir(user_id, session_id)
    target = (sdir / name).resolve()
    if sdir not in target.parents:
        raise HTTPException(400, "Invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "File not found")
    size = target.stat().st_size
    truncated = size > MAX_VIEW_BYTES
    try:
        if truncated:
            with target.open("rb") as f:
                data = f.read(MAX_VIEW_BYTES)
            content = data.decode("utf-8", errors="replace")
        else:
            content = target.read_text("utf-8")
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

        with app.state.pool.connection() as conn:
            _verify_thread(conn, user_id, req.session_id, req.thread_id)

        try:
            pre_state = agent.get_state(config)
        except Exception as e:
            print(f"[/chat] get_state failed: {e!r}", flush=True)
            traceback.print_exc()
            raise HTTPException(500, f"Checkpoint state error: {e}")
        pre_msgs = (pre_state.values or {}).get("messages", []) if pre_state else []
        pre_count = len(pre_msgs)

        try:
            result = agent.invoke(
                {"messages": [HumanMessage(content=req.message)]},
                config=config,
            )
        except Exception as e:
            msg = str(e) or e.__class__.__name__
            print(f"[/chat] model invoke failed: {msg}", flush=True)
            traceback.print_exc()
            low = msg.lower()
            if "429" in msg or "rate" in low or "quota" in low:
                raise HTTPException(429, "Model rate-limited. Wait a minute and retry, or add OpenRouter credit.")
            if "401" in msg or "unauthorized" in low or "api key" in low:
                raise HTTPException(401, f"Model auth failed (check OPENROUTER_API_KEY): {msg[:200]}")
            raise HTTPException(500, f"Model error: {msg[:300]}")

        reply = ""
        new_messages = result["messages"][pre_count:]
        try:
            with app.state.pool.connection() as conn:
                for msg in new_messages:
                    if isinstance(msg, HumanMessage):
                        _record_message(
                            conn, req.session_id, req.thread_id, user_id, "user", msg.content
                        )
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
