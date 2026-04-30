# Contributing

## Branching

- `main` — protected, deployable.
- `dev` — integration branch. All feature PRs target `dev`.
- `feature/<short-name>` — feature branches off `dev`.
- `fix/<short-name>` — bug-fix branches off `dev`.

## Commit messages

Use conventional prefixes: `feat:`, `fix:`, `docs:`, `refactor:`, `chore:`, `ci:`.

Example: `feat: add calculator tool to agent registry`

## Project structure (must not break)

```
agent/
├── api.py                # FastAPI entrypoint — all HTTP routes here
├── Tools/                # All agent tools live here
│   ├── __init__.py       # Must export all_tools
│   └── *_tool.py         # One @tool-decorated function per file
├── ui/                   # Next.js app — never import backend code from here
│   ├── app/              # App Router pages
│   └── lib/              # Shared client utilities (e.g. authFetch)
├── requirements.txt      # Backend deps
└── README.md             # Always reflects current setup
```

Rules:
- New tools go in `Tools/<name>_tool.py` and **must** be added to `all_tools` in [Tools/__init__.py](../Tools/__init__.py).
- Backend code never imports from `ui/`. Frontend never imports from Python.
- All HTTP routes that touch user data **must** declare `user_id: str = Depends(get_current_user)` and scope DB queries by `user_id`.
- LangGraph `thread_id` must always be prefixed with the user id: `f"{user_id}:{session_id}"`.
- Frontend calls to `/api/*` (other than login) **must** go through `authFetch`, not raw `fetch`.

## Secrets

- Never commit `.env`, `.env.local`, or any file with credentials.
- Use `.env.example` / `.env.local.example` to document required keys.
- Configure GitHub Actions secrets (`ANTHROPIC_API_KEY` for the AI review action) under repo Settings → Secrets and variables → Actions.

## Pull requests

1. Push your branch and open a PR against `dev`.
2. Fill out the PR template completely.
3. CI must pass:
   - Structure & secret checks
   - Backend lint (ruff) + format
   - Frontend lint, type check, build
4. The Claude AI review action will post comments — address real issues before requesting human review.
5. At least 1 approval from a CODEOWNER required to merge.
6. Squash-merge into `dev`. `dev` → `main` is a release PR.

## Local dev

See the [README](../README.md#running) for full setup. TL;DR:

```powershell
# Backend
.venv\Scripts\activate
uvicorn api:app --reload --port 8000

# Frontend (separate terminal)
cd ui
npm run dev
```
