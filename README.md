# FYP Agent

A full-stack LangGraph agent with per-user authentication, persistent multi-project chat history, and a Next.js chat UI.

## Stack

- **Backend** — FastAPI + LangGraph (`create_agent`) + LangChain
- **Model** — OpenRouter (chat + vision)
- **Persistence** — Postgres (LangGraph checkpoints + sessions/threads/messages/workspaces, plus `github_credentials`)
- **Object storage** — S3-compatible: MinIO locally via docker-compose, AWS S3 / R2 in prod (`boto3`)
- **Auth** — Supabase Auth (email/password, JWT verified server-side via JWKS)
- **Sandboxed workspaces** — pluggable backend (`SANDBOX_BACKEND=docker|e2b`): local Docker socket for solo dev (`docker` SDK + the `acm-workspace` image), E2B Firecracker microVMs for multi-user (`e2b` SDK)
- **File parsing** — `pypdf`, `python-docx`, `openpyxl`
- **GitHub integration** — `PyGithub` with per-user PATs (read, link, and server-side repo creation)
- **Frontend** — Next.js 14 (App Router) + Tailwind + TypeScript
- **Markdown** — `react-markdown` + `remark-gfm`

See [architecture.md](architecture.md) for the full component breakdown.

## Project structure

```
agentic_context_management/
├── backend/                  # FastAPI + LangGraph agent
│   ├── api.py                # FastAPI app, auth, sessions/threads/messages, files, chat, workspaces, github
│   ├── agent_callbacks.py    # LangGraph callbacks
│   ├── storage.py            # S3-compatible bucket facade (MinIO / S3)
│   ├── sandbox_client.py     # SandboxBackend ABC + DockerBackend + E2BBackend + factory
│   ├── github_client.py      # PyGithub wrapper, per-user PAT storage, repo creation
│   ├── Tools/                # Tools registered with the agent
│   │   ├── __init__.py       # all_tools registry
│   │   ├── _paths.py         # config-scoped helpers (user_id, session_id, workspace_ref)
│   │   ├── calculator_tool.py
│   │   ├── weather_tool.py
│   │   ├── list_files_tool.py
│   │   ├── read_file_tool.py
│   │   ├── write_file_tool.py
│   │   └── shell_tool.py     # run_shell inside the session's sandboxed workspace
│   ├── requirements.txt
│   ├── pyproject.toml
│   └── .env.example
├── ui/                       # Next.js frontend
│   ├── app/
│   │   ├── page.tsx          # landing
│   │   ├── app/              # chat workspace
│   │   ├── login/            # login + signup
│   │   ├── layout.tsx
│   │   └── globals.css
│   ├── lib/supabase.ts
│   └── .env.local.example
├── sandbox/                  # Workspace runtime + diagnostic scripts
│   ├── Dockerfile            # `acm-workspace` image: Python 3.13 + Node 20 + git
│   ├── build.sh              # builds + smoke-tests the image
│   ├── smoke_test.py         # exercises SandboxBackend end-to-end (create→exec→destroy)
│   ├── test_chat_flow.py     # exercises the lazy-create + run_shell wiring
│   └── diagnose_model.py     # one-shot OpenRouter reachability check
├── db/
│   └── init.sql              # Postgres schema, auto-loaded by docker-compose
├── docker-compose.yml        # Postgres + MinIO (+ bucket init)
├── .env.example              # docker-compose overrides
├── architecture.md
├── PROJECT.md                # roadmap + phase plan
└── README.md
```

## Prerequisites

- Python 3.11
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- Node.js 18+
- Docker (for local Postgres + MinIO via docker-compose)
- A Supabase project (free tier works) — only used for Auth / JWT
- An OpenRouter API key

## Setup

### 1. Services (Postgres + MinIO)

Optionally copy `.env.example` to `.env` at the repo root if you want to override the docker-compose defaults (ports, credentials, bucket name).

Start the local services:

```bash
docker compose up -d
```

This brings up:

- Postgres on `localhost:5432` (DB `acm`, user/password `postgres`/`postgres`). The schema in [db/init.sql](db/init.sql) is loaded automatically on first boot.
- MinIO on `localhost:9000` (API) and `localhost:9001` (console). The `project-files` bucket is pre-created by the `minio-init` one-shot container.

LangGraph's checkpoint tables are created on first backend startup via `PostgresSaver.setup()`.

### 2. Workspace image (Docker backend only)

Project sessions get a sandboxed workspace from the configured `SANDBOX_BACKEND`. The default (`docker`) needs the `acm-workspace` image — Python 3.13 + Node 20 + git + build tools — built locally once:

```bash
./sandbox/build.sh
```

The build script also runs a smoke test (`python -V`, `node -v`, `git --version`) so failures surface immediately. Skip this step if you set `SANDBOX_BACKEND=e2b` instead.

> The Docker backend mounts the host's Docker socket and is only safe for solo / localhost use — a container escape gives host root. Flip to `e2b` before exposing this app to anyone else. See [PROJECT.md](PROJECT.md).

### 3. Backend

Copy `backend/.env.example` to `backend/.env` and fill in the secrets. Minimum required for local dev:

```env
OPENROUTER_API_KEY=sk-or-v1-...
SUPABASE_URL=https://<ref>.supabase.co
SUPABASE_DB_URL=postgresql://postgres:postgres@localhost:5432/acm

# Object storage — matches docker-compose defaults
S3_ENDPOINT_URL=http://localhost:9000
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_REGION=us-east-1
S3_BUCKET=project-files

# Sandboxed workspaces
SANDBOX_BACKEND=docker          # or 'e2b' for multi-user; needs E2B_API_KEY
E2B_API_KEY=                    # leave blank for the docker backend
WORKSPACE_TTL_HOURS=24
WORKSPACE_IDLE_PAUSE_MIN=15
WORKSPACE_MAX_PER_USER=3
WORKSPACE_IMAGE=acm-workspace:latest

# Optional — LangSmith tracing
LANGSMITH_TRACING=false
LANGSMITH_API_KEY=
LANGSMITH_PROJECT=FYP
```

Create a virtualenv at the repo root and install Python dependencies with `uv`:

```bash
uv venv --python 3.11
source .venv/bin/activate
uv pip install -r backend/requirements.txt
```

### 4. Frontend

Copy `ui/.env.local.example` to `ui/.env.local` and fill in your Supabase project keys:

```env
NEXT_PUBLIC_SUPABASE_URL=https://<ref>.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=<publishable-key>
```

Install dependencies:

```bash
cd ui
npm install
```

## Running

Make sure `docker compose up -d` is running, then open two terminals.

**Backend** (terminal 1):

```bash
source .venv/bin/activate
cd backend
uvicorn api:app --reload --port 8000
```

**Frontend** (terminal 2):

```bash
cd ui
npm run dev
```

Then open http://localhost:3000.

> The Next.js dev server proxies `/api/*` to `http://localhost:8000` (see [ui/next.config.mjs](ui/next.config.mjs)). Both servers must be running.

## How it works

- A **project** (session) groups one or more **threads**. Each thread is its own conversation. Sessions are typed `chat` or `project`; only `project` sessions get a sandboxed workspace.
- Every chat request is authenticated with a Supabase JWT, verified server-side via JWKS.
- LangGraph state is checkpointed in Postgres, scoped by `{user_id}:{session_id}` so users can never see each other's state.
- User isolation is enforced in Python (`_verify_session`, `_verify_thread`, and the `user_id` prefix on every S3 key). On Supabase deployments RLS adds a second layer; the local Postgres image does not enable RLS because the backend connects with full privileges.
- Uploaded files live in S3 under `{user_id}/{session_id}/{filename}` so the agent's file tools are naturally scoped to the project.
- **Sandboxed workspaces** lazy-create on the first chat turn in a project session — either a Docker container or an E2B microVM depending on `SANDBOX_BACKEND`. The workspace auto-clones a linked GitHub repo (or `git init`s) on first boot, so rollback works from minute one. The agent reaches it through the `run_shell` tool. A GC loop pauses idle workspaces (>15 min) and destroys expired ones (>24h after last use). Per-user concurrency is capped (default 3).
- Project creation can simultaneously **create a new GitHub repo** for you (or link to an existing one) via `POST /sessions` with `github_mode=new_repo|link_existing` — needs a PAT with the `repo` scope.
- The frontend fires the `/api/chat` request and polls `/api/sessions/<sid>/threads/<tid>/history` until a new assistant message appears, so transient model errors (e.g. free-tier rate limits) don't surface as UI errors.

## Choosing a model

There are **two model-selection paths** depending on whether you've configured an LLM provider:

### A. With configured providers (recommended)

Open the sidebar → **LLM Providers** → add one or more of: OpenAI, Anthropic, AWS Bedrock, Azure OpenAI, Google AI Studio, OpenRouter.

For each provider you supply credentials and a model ID. The "Fetch available" button in the Add/Edit modal pulls a live model list when the provider exposes one (OpenAI, OpenRouter, Bedrock, Azure deployments, Google). Marking one as ★ default sets it as the fallback for new sessions. Credentials are stored Fernet-encrypted in Postgres using the `MCP_SECRET_KEY` env var.

The chat header's top-right picker then lists **your configured providers** (instead of OpenRouter's free-model catalog). Selecting one writes `preferred_provider_id` on the current session — different sessions can use different providers in parallel.

Resolution order on every `/chat` turn:

1. The session's `preferred_provider_id` (Phase F picker)
2. The user's default provider (`is_default = true`)
3. The env-var path (`OPENROUTER_API_KEY` + `CHAT_MODEL`) — legacy / zero-config fallback

### B. Zero-config (legacy)

If you have **no** providers configured, the chat header reverts to the old OpenRouter free-model dropdown populated from `GET /api/models`. The backend uses `OPENROUTER_API_KEY` + `CHAT_MODEL` directly. This is what runs out-of-the-box on a fresh checkout.

### Provider credential reference

| Provider | Required fields | Where to get them |
|---|---|---|
| OpenRouter | `api_key` | https://openrouter.ai/keys |
| OpenAI | `api_key` (optional `organization`) | https://platform.openai.com/api-keys |
| Anthropic | `api_key` | https://console.anthropic.com/settings/keys |
| AWS Bedrock | `aws_access_key_id`, `aws_secret_access_key`, `aws_region` | IAM user with `AmazonBedrockFullAccess` and Bedrock model access enabled in the chosen region |
| Azure OpenAI | `api_key`, `azure_endpoint` (e.g. `https://my-rg.openai.azure.com`), `api_version` (optional, defaults to `2024-10-21`) | Azure portal → Cognitive Services → Keys and Endpoint. Model ID is the **deployment name**, not the underlying model. |
| Google AI Studio | `api_key` | https://aistudio.google.com/apikey |

Adapter implementations live in [backend/providers/](backend/providers/) — one file per provider following the `LLMProvider` ABC in `base.py`.

## Adding a tool

1. Create `backend/Tools/<name>_tool.py` with a `@tool`-decorated function.
2. Import it in [backend/Tools/__init__.py](backend/Tools/__init__.py) and add it to `all_tools`.
3. Restart the backend.

## Troubleshooting

- **`KeyError: 'SUPABASE_DB_URL'` on startup** — `backend/.env` is missing.
- **`ModuleNotFoundError: No module named 'jwt'`** — venv isn't activated. Look for `(.venv)` in your prompt before running `uvicorn`.
- **`supabaseUrl is required` in the browser** — `ui/.env.local` is missing.
- **`ECONNREFUSED 127.0.0.1:8000`** — backend isn't running, or you ran `uvicorn` from the wrong directory (run it from `backend/`).
- **`could not connect to server` / Postgres errors on startup** — docker-compose isn't running. Start it with `docker compose up -d`.
- **`FATAL: role "postgres" does not exist`** — a host-level Postgres (Postgres.app / Homebrew) is already bound to `127.0.0.1:5432` and is intercepting the connection before it reaches Docker. Remap the container port: set `POSTGRES_PORT=5433` in the root `.env`, update `SUPABASE_DB_URL` in `backend/.env` to `postgresql://postgres:postgres@localhost:5433/acm`, and `docker compose down && docker compose up -d`.
- **HTTP 429 from the model** — OpenRouter free-tier daily quota. Wait or add credit. Run [sandbox/diagnose_model.py](sandbox/diagnose_model.py) for a one-shot reachability check.
- **`Workspace image not found: acm-workspace:latest`** — you haven't built the workspace image. Run `./sandbox/build.sh`.
- **Agent says "this chat does not have a sandboxed workspace attached"** — the session is `kind='chat'`, not `'project'`. Workspaces are only provisioned for project sessions. Create a new project (or update the row's `kind` column).
- **`Cannot connect to the Docker daemon`** — Docker Desktop / colima isn't running. The backend connects via the host socket when `SANDBOX_BACKEND=docker`.
- **Next.js logs `Failed to proxy ... socket hang up` / `ECONNRESET`** on every `/api/*` call — Node 17+ resolves `localhost` to `::1` (IPv6) first on macOS, and our uvicorn only binds IPv4. [ui/next.config.mjs](ui/next.config.mjs) pins the proxy to `127.0.0.1:8000` to avoid this. If you've customised the proxy target back to `localhost`, change it back, or run uvicorn with `--host ::` to listen on both stacks.
