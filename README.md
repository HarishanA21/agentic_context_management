# FYP Agent

A full-stack LangGraph agent with per-user authentication, persistent multi-project chat history, and a Next.js chat UI.

## Stack

- **Backend** — FastAPI + LangGraph (`create_agent`) + LangChain
- **Model** — OpenRouter (chat + vision)
- **Persistence** — Postgres (LangGraph checkpoints + sessions/threads/messages, plus `github_credentials`)
- **Object storage** — S3-compatible: MinIO locally via docker-compose, AWS S3 / R2 in prod (`boto3`)
- **Auth** — Supabase Auth (email/password, JWT verified server-side via JWKS)
- **File parsing** — `pypdf`, `python-docx`, `openpyxl`
- **GitHub integration** — `PyGithub` with per-user PATs
- **Frontend** — Next.js 14 (App Router) + Tailwind + TypeScript
- **Markdown** — `react-markdown` + `remark-gfm`

See [architecture.md](architecture.md) for the full component breakdown.

## Project structure

```
agentic_context_management/
├── backend/                # FastAPI + LangGraph agent
│   ├── api.py              # FastAPI app, auth, sessions/threads/messages, files, chat, github
│   ├── agent_callbacks.py  # LangGraph callbacks
│   ├── storage.py          # S3-compatible bucket facade (MinIO / S3)
│   ├── github_client.py    # PyGithub wrapper, per-user PAT storage
│   ├── Tools/              # Tools registered with the agent
│   │   ├── __init__.py     # all_tools registry
│   │   ├── _paths.py
│   │   ├── calculator_tool.py
│   │   ├── weather_tool.py
│   │   ├── list_files_tool.py
│   │   ├── read_file_tool.py
│   │   └── write_file_tool.py
│   ├── requirements.txt
│   ├── pyproject.toml
│   └── .env.example
├── ui/                     # Next.js frontend
│   ├── app/
│   │   ├── page.tsx        # landing
│   │   ├── app/            # chat workspace
│   │   ├── login/          # login + signup
│   │   └── globals.css
│   ├── lib/supabase.ts
│   └── .env.local.example
├── db/
│   └── init.sql            # Postgres schema, auto-loaded by docker-compose
├── docker-compose.yml      # Postgres + MinIO (+ bucket init)
├── .env.example            # docker-compose overrides
├── architecture.md
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

### 2. Backend

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

### 3. Frontend

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

- A **project** (session) groups one or more **threads**. Each thread is its own conversation.
- Every chat request is authenticated with a Supabase JWT, verified server-side via JWKS.
- LangGraph state is checkpointed in Postgres, scoped by `{user_id}:{session_id}` so users can never see each other's state.
- User isolation is enforced in Python (`_verify_session`, `_verify_thread`, and the `user_id` prefix on every S3 key). On Supabase deployments RLS adds a second layer; the local Postgres image does not enable RLS because the backend connects with full privileges.
- Uploaded files live in S3 under `{user_id}/{session_id}/{filename}` so the agent's file tools are naturally scoped to the project.
- The frontend fires the `/api/chat` request and polls `/api/sessions/<sid>/threads/<tid>/history` until a new assistant message appears, so transient model errors (e.g. free-tier rate limits) don't surface as UI errors.

## Choosing a model

The chat header has a model picker populated from `GET /api/models`, which proxies OpenRouter's catalog filtered to `:free` models (cached server-side for 10 minutes). Selection persists in `localStorage` and is sent on every `/chat` and `/context` request. The backend builds one LangGraph agent per model on first use and caches it; the same `PostgresSaver` checkpointer is shared across models so conversation history is preserved when you switch.

If `OPENROUTER_API_KEY` isn't set or the catalog fetch fails, the picker stays empty and chat falls back to the backend's `CHAT_MODEL` env default.

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
- **HTTP 429 from the model** — OpenRouter free-tier daily quota. Wait or add credit.
