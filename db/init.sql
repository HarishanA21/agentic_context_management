-- Local Postgres schema for the agentic_context_management backend.
-- Runs once when the postgres container's data volume is first created.
--
-- Differences from the Supabase deployment:
--   * user_id has no FK to auth.users (Supabase Auth lives outside this DB).
--     The backend trusts the user_id from the Supabase JWT it verifies in Python.
--   * No RLS policies — local backend connects with full privileges and
--     enforces user scoping in Python (see _verify_session in api.py).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS sessions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid NOT NULL,
    name            text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    github_owner    text,
    github_repo     text,
    github_branch   text
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS threads (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id     uuid NOT NULL,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_threads_session ON threads(session_id, created_at);

CREATE TABLE IF NOT EXISTS messages (
    id               bigserial PRIMARY KEY,
    session_id       uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    thread_id        uuid NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    user_id          uuid NOT NULL,
    role             text NOT NULL,
    content          text NOT NULL,
    tool_name        text,
    tool_calls_json  jsonb,
    tokens           integer NOT NULL DEFAULT 0,
    input_tokens     integer NOT NULL DEFAULT 0,
    output_tokens    integer NOT NULL DEFAULT 0,
    thinking_tokens  integer NOT NULL DEFAULT 0,
    langgraph_id     text,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(session_id, thread_id, id);

CREATE TABLE IF NOT EXISTS github_credentials (
    user_id          uuid PRIMARY KEY,
    token            text NOT NULL,
    github_username  text,
    updated_at       timestamptz NOT NULL DEFAULT now()
);
