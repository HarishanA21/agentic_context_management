import json
import os
from contextlib import asynccontextmanager
from typing import Optional

import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from jwt import PyJWKClient
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from Tools import all_tools

load_dotenv()

DB_URL = os.environ["SUPABASE_DB_URL"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
JWKS_URL = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"
_jwks_client = PyJWKClient(JWKS_URL)

model = ChatOpenAI(
    model="z-ai/glm-4.5-air:free",
    openai_api_key=os.getenv("OPENROUTER_API_KEY"),
    openai_api_base="https://openrouter.ai/api/v1",
    max_tokens=2048,
    temperature=0.1,
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
):
    conn.execute(
        """
        INSERT INTO messages
            (session_id, thread_id, user_id, role, content, tool_name, tool_calls_json)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            session_id,
            thread_id,
            user_id,
            role,
            content,
            tool_name,
            json.dumps(tool_calls) if tool_calls else None,
        ),
    )


@app.get("/sessions")
def list_sessions(user_id: str = Depends(get_current_user)):
    with app.state.pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, name, created_at FROM sessions WHERE user_id = %s ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [
        {"id": str(r[0]), "name": r[1], "created_at": r[2].isoformat()}
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
        "default_thread": {
            "id": str(t[0]),
            "session_id": str(sid),
            "name": t[1],
            "created_at": t[2].isoformat(),
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
            "SELECT id, name, created_at FROM threads "
            "WHERE session_id = %s AND user_id = %s ORDER BY created_at ASC",
            (session_id, user_id),
        ).fetchall()
    return [
        {
            "id": str(r[0]),
            "session_id": session_id,
            "name": r[1],
            "created_at": r[2].isoformat(),
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
    }


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
    agent = app.state.agent
    # Scope LangGraph thread by user+session to prevent cross-user collisions.
    config = {"configurable": {"thread_id": f"{user_id}:{req.session_id}"}}

    with app.state.pool.connection() as conn:
        _verify_thread(conn, user_id, req.session_id, req.thread_id)

    pre_state = agent.get_state(config)
    pre_msgs = (pre_state.values or {}).get("messages", []) if pre_state else []
    pre_count = len(pre_msgs)

    try:
        result = agent.invoke(
            {"messages": [HumanMessage(content=req.message)]},
            config=config,
        )
    except Exception as e:
        msg = str(e)
        if "429" in msg or "rate" in msg.lower():
            raise HTTPException(429, "Model rate-limited. Wait a minute and retry, or add OpenRouter credit.")
        raise HTTPException(500, f"Model error: {msg[:200]}")

    reply = ""
    new_messages = result["messages"][pre_count:]
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

    return {"reply": reply}
