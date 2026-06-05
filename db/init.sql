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

-- ── MCP server inventory (Phase 5) ───────────────────────────────────────────
-- Each row is a user's configuration of one MCP server — either an entry
-- from the shipped catalog (`is_custom=false`, `catalog_slug` non-null) or
-- a user-defined custom server (`is_custom=true`).
--
-- Disabling a catalog row keeps the row so saved env-vars survive a re-enable.
-- See MCP_INVENTORY.md for the full design.
CREATE TABLE IF NOT EXISTS mcp_servers (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            uuid NOT NULL,
    catalog_slug       text,
    is_custom          boolean NOT NULL DEFAULT false,
    name               text NOT NULL,
    enabled            boolean NOT NULL DEFAULT false,
    transport          text NOT NULL
                            CHECK (transport IN
                                ('stdio', 'streamable_http', 'sse', 'http')),
    -- stdio
    command            text,
    args_json          jsonb,
    -- http-family
    endpoint_url       text,
    auth_kind          text
                            CHECK (auth_kind IS NULL
                                OR auth_kind IN
                                    ('none', 'bearer', 'api_key_header',
                                     'api_key_env', 'oauth')),
    auth_header        text,
    -- Fernet-encrypted secret material (bearer token, header value, or
    -- JSON map of env-var names→values). Never returned by the API.
    secret_blob        text,
    -- last-discovered tool list, cached for the UI. Refreshed on
    -- successful connect; agent never reads this (uses live discovery).
    tools_json         jsonb,
    last_connected_at  timestamptz,
    last_error         text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, catalog_slug)
);

CREATE INDEX IF NOT EXISTS idx_mcp_servers_user
    ON mcp_servers(user_id);
CREATE INDEX IF NOT EXISTS idx_mcp_servers_user_enabled
    ON mcp_servers(user_id) WHERE enabled;


-- ── Skills ──────────────────────────────────────────────────────────────
-- Toggleable instruction bundles folded into the agent's system prompt
-- (claude.ai-style "+" → Skills). Two kinds:
--   * catalog skill  — is_custom=false, catalog_slug set. The row only tracks
--                       `enabled`; name/description/instructions live in code
--                       (backend/skills_catalog.py) so edits auto-deploy.
--   * custom skill    — is_custom=true, catalog_slug NULL. Carries its own
--                       name/description/instructions authored by the user.
-- Disabling a catalog skill keeps the row so the toggle state survives.
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
);

CREATE INDEX IF NOT EXISTS idx_skills_user ON skills(user_id);
CREATE INDEX IF NOT EXISTS idx_skills_user_enabled
    ON skills(user_id) WHERE enabled;


-- ── Plugins ─────────────────────────────────────────────────────────────
-- Per-user enabled state for code-defined plugins. Each enabled plugin adds
-- one or more real tools to the agent's toolbox (see backend/plugins_catalog.py
-- and backend/plugin_tools.py). Plugins are not user-authored, so a row just
-- tracks `enabled` for a catalog slug.
CREATE TABLE IF NOT EXISTS plugins (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL,
    catalog_slug  text NOT NULL,
    enabled       boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, catalog_slug)
);

CREATE INDEX IF NOT EXISTS idx_plugins_user_enabled
    ON plugins(user_id) WHERE enabled;


-- ── LLM providers ───────────────────────────────────────────────────────
-- Per-user multi-provider configs (OpenAI, Anthropic, Bedrock, etc.).
-- Credentials are Fernet-encrypted JSON in `credentials_blob`. At most one
-- row per user can have `is_default = true` (enforced in app code).

CREATE TABLE IF NOT EXISTS llm_providers (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            uuid NOT NULL,
    slug               text NOT NULL,        -- 'openai' | 'anthropic' | 'openrouter' | ...
    label              text NOT NULL,        -- user-set nickname ("My OpenAI key")
    model_id           text NOT NULL,        -- e.g. 'gpt-4o-mini'
    -- Fernet-encrypted JSON dict of credentials (api_key, region, etc.).
    -- Never returned by the API — endpoints redact to has_credentials: bool.
    credentials_blob   text NOT NULL,
    is_default         boolean NOT NULL DEFAULT false,
    last_error         text,
    last_tested_at     timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, label)
);

CREATE INDEX IF NOT EXISTS idx_llm_providers_user
    ON llm_providers(user_id);
CREATE INDEX IF NOT EXISTS idx_llm_providers_user_default
    ON llm_providers(user_id) WHERE is_default;


-- ── Per-session preferred provider ──────────────────────────────────────
-- Phase F: a session can override the user-level default provider. NULL
-- means "use the user's default" (falls through to is_default in
-- llm_providers). FK uses ON DELETE SET NULL so deleting the provider
-- silently reverts affected sessions to the user default.
ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS preferred_provider_id uuid
        REFERENCES llm_providers(id) ON DELETE SET NULL;


-- Phase G follow-up: per-provider temperature and max_tokens overrides.
-- NULL means "use the adapter's built-in default" (currently env-driven).
ALTER TABLE llm_providers
    ADD COLUMN IF NOT EXISTS temperature double precision;
ALTER TABLE llm_providers
    ADD COLUMN IF NOT EXISTS max_tokens integer;
