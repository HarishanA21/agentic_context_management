# FYP Agent

A full-stack LangGraph agent with per-user authentication, persistent multi-project chat history, and a Next.js chat UI.

## Stack

- **Backend** — FastAPI + LangGraph (`create_agent`) + LangChain
- **Model** — `z-ai/glm-4.5-air:free` via OpenRouter
- **Persistence** — Supabase Postgres (LangGraph checkpoints + sessions/threads/messages tables)
- **Auth** — Supabase Auth (email/password, JWT verified server-side via JWKS)
- **Frontend** — Next.js 14 (App Router) + Tailwind + TypeScript
- **Markdown** — `react-markdown` + `remark-gfm`

## Project structure

```
agent/
├── backend/                # All Python / FastAPI code
│   ├── api.py              # FastAPI app, auth, sessions/threads/messages, /chat
│   ├── agent_callbacks.py  # LangGraph callbacks
│   ├── Tools/              # Tools registered with the agent
│   │   ├── __init__.py     # all_tools registry
│   │   ├── weather_tool.py
│   │   └── calculator_tool.py
│   ├── requirements.txt
│   ├── pyproject.toml
│   ├── .env.example
│   └── .env                # backend secrets (not committed)
├── ui/                     # Next.js frontend
│   ├── app/
│   │   ├── page.tsx        # main chat UI
│   │   ├── app/page.tsx    # chat workspace
│   │   ├── login/          # login + signup
│   │   └── globals.css     # markdown + theme styles
│   └── lib/                # shared client utilities
└── README.md
```

## Prerequisites

- Python 3.13
- Node.js 18+
- A Supabase project (free tier works)
- An OpenRouter API key

## Setup

### 1. Backend

Create `backend/.env`:

```env
OPENROUTER_API_KEY=sk-or-v1-...
SUPABASE_DB_URL=postgresql://postgres.<ref>:<password>@aws-...pooler.supabase.com:5432/postgres
SUPABASE_URL=https://<ref>.supabase.co

# Optional — LangSmith tracing
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_pt_...
LANGSMITH_PROJECT=FYP
```

Install dependencies into a virtualenv (kept at the project root):

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r backend/requirements.txt
```

### 2. Database schema

Run this once in the Supabase SQL editor:

```sql
create table public.sessions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null,
  created_at timestamptz not null default now()
);

create table public.threads (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null references public.sessions(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null,
  created_at timestamptz not null default now()
);

create table public.messages (
  id bigserial primary key,
  session_id uuid not null references public.sessions(id) on delete cascade,
  thread_id uuid not null references public.threads(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  role text not null,
  content text not null,
  tool_name text,
  tool_calls_json jsonb,
  created_at timestamptz not null default now()
);

create index idx_messages_thread on public.messages(session_id, thread_id, id);

alter table public.sessions enable row level security;
alter table public.threads  enable row level security;
alter table public.messages enable row level security;

create policy "own sessions" on public.sessions for all
  using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "own threads"  on public.threads  for all
  using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "own messages" on public.messages for all
  using (auth.uid() = user_id) with check (auth.uid() = user_id);
```

LangGraph's checkpoint tables are created automatically on first startup.

### 3. Frontend

Create `ui/.env.local`:

```env
NEXT_PUBLIC_SUPABASE_URL=https://<ref>.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=<publishable-key>
```

Install dependencies:

```powershell
cd ui
npm install
```

## Running

Open two terminals.

**Backend** (terminal 1):

```powershell
.venv\Scripts\activate
cd backend
uv pip install -r requirements.txt
uvicorn api:app --reload --port 8000
```

**Frontend** (terminal 2):

```powershell
cd ui
npm run dev
```

Then open http://localhost:3000.

> The Next.js dev server proxies `/api/*` to `http://localhost:8000` (see [ui/next.config.mjs](ui/next.config.mjs)). Both servers must be running.

## How it works

- A **project** (session) groups one or more **threads**. Each thread is its own conversation.
- Every chat request is authenticated with a Supabase JWT, verified server-side via JWKS.
- LangGraph state is checkpointed in Postgres, scoped by `{user_id}:{session_id}` so users can never see each other's state.
- Row Level Security on `sessions` / `threads` / `messages` is a second layer of isolation.
- The frontend fires the `/api/chat` request and polls `/api/sessions/<sid>/threads/<tid>/history` until a new assistant message appears, so transient model errors (e.g. free-tier rate limits) don't surface as UI errors.

## Adding a tool

1. Create `backend/Tools/<name>_tool.py` with a `@tool`-decorated function.
2. Import it in [backend/Tools/__init__.py](backend/Tools/__init__.py) and add it to `all_tools`.
3. Restart the backend.

## Troubleshooting

- **`KeyError: 'SUPABASE_DB_URL'` on startup** — `backend/.env` is missing.
- **`ModuleNotFoundError: No module named 'jwt'`** — venv isn't activated. Look for `(agent)` in your prompt before running `uvicorn`.
- **`supabaseUrl is required` in the browser** — `ui/.env.local` is missing.
- **`ECONNREFUSED 127.0.0.1:8000`** — backend isn't running, or you ran `uvicorn` from the wrong directory (run it from `backend/`).
- **HTTP 429 from the model** — OpenRouter free-tier daily quota (200 req/day, shared across all `:free` models). Wait or add credit.
