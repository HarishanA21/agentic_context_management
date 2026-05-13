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
    kind            text NOT NULL DEFAULT 'chat'
                         CHECK (kind IN ('chat', 'project')),
    -- 'auto' lets the agent act freely; 'confirm' makes it ask before
    -- write_project_file or run_shell. Prompt-based enforcement (not hard
    -- interrupts) — see SYSTEM_PROMPT in backend/api.py.
    mode            text NOT NULL DEFAULT 'auto'
                         CHECK (mode IN ('auto', 'confirm')),
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

-- ── Phase 1: sandboxed workspaces ────────────────────────────────────────────
-- One row per workspace (a sandboxed container or microVM). Bound to a session
-- so that destroying the session cascades to its workspaces. `backend` records
-- which SandboxBackend implementation owns this row (`docker` vs `e2b`);
-- `backend_ref` is the implementation-specific handle (container id or
-- sandbox id). See backend/sandbox_client.py.

CREATE TABLE IF NOT EXISTS workspaces (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL,
    session_id    uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    backend       text NOT NULL CHECK (backend IN ('docker', 'e2b')),
    backend_ref   text NOT NULL,
    status        text NOT NULL DEFAULT 'running'
                       CHECK (status IN ('running', 'paused', 'destroyed')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL DEFAULT now() + interval '24 hours',
    UNIQUE (backend, backend_ref)
);

-- GC loop scans for workspaces past expires_at, or idle past the pause cutoff.
CREATE INDEX IF NOT EXISTS idx_workspaces_gc
    ON workspaces(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_workspaces_idle
    ON workspaces(status, last_used_at);

-- Per-user concurrency cap check: count active workspaces for a user.
CREATE INDEX IF NOT EXISTS idx_workspaces_user_active
    ON workspaces(user_id, status);

-- Session → workspace lookup (lazy-create finds an existing running/paused one).
CREATE INDEX IF NOT EXISTS idx_workspaces_session
    ON workspaces(session_id, status);

-- Append-only log of git commits made inside a workspace. Synced from the
-- workspace's `git log` at the end of each chat turn — so the source of
-- truth is the workspace's git history; this table is a queryable mirror.
-- See backend/api.py `_sync_workspace_commits`.
CREATE TABLE IF NOT EXISTS workspace_commits (
    id           bigserial PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    session_id   uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id      uuid NOT NULL,
    sha          text NOT NULL,
    message      text NOT NULL,
    pushed_at    timestamptz,
    reverted_at  timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, sha)
);

CREATE INDEX IF NOT EXISTS idx_workspace_commits_session
    ON workspace_commits(session_id, id DESC);
