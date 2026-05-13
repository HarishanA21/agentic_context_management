# FYP Agent — Project Roadmap

## Vision

A self-hosted, Codex-like coding platform delivered as a **web application** (Next.js in the browser, FastAPI + LangGraph on the server). Per-user auth, project-scoped context, sandboxed code execution against real git repos, autonomous task runs with live observability, PR creation and review, parallel agents — all running server-side and surfaced through the web UI. The current chat agent stays; the platform plan is **additive**, building the missing capabilities around it.

**Not building:** native desktop apps, browser extensions, or anything requiring local-filesystem access — see [Out of scope](#out-of-scope). Every capability the user sees is reachable from a browser tab.

## Stack

- **Frontend** — Next.js 14 (App Router) + Tailwind + TypeScript
- **Backend** — FastAPI + LangGraph (`create_agent`) + LangChain
- **Auth** — Supabase Auth (email/password, JWT verified via JWKS server-side)
- **Persistence** — Postgres (local via `docker-compose` or Supabase) — sessions, threads, messages, LangGraph checkpoints, GitHub credentials
- **File storage** — S3-compatible (MinIO locally via `docker-compose` or AWS S3) — bucket `project-files`, scoped by `user_id/session_id/`
- **Main chat model** — `meta-llama/llama-3.3-70b-instruct:free` via OpenRouter (configurable via `CHAT_MODEL` env var)
- **Vision model** — `meta-llama/llama-3.2-11b-vision-instruct:free` (image reads only)

## ✅ Done

### Core
- [x] Supabase Auth (signup, login, JWT-protected backend)
- [x] Persistent multi-project, multi-thread chat history (Postgres)
- [x] LangGraph agent with checkpointer (state survives restarts, per-user thread scoping)
- [x] Row Level Security (RLS) on all user data + storage bucket (Supabase deployment)
- [x] Project / chat distinction in UI (kind = "project" with multiple chats, or "chat" with one)
- [x] Per-row sidebar menu on projects, chats, and chats inside projects (rename, delete, move to project, share)
- [x] Centered confirm + prompt modals
- [x] Right-side file viewer with line numbers
- [x] Markdown rendering (react-markdown + remark-gfm)
- [x] Auto-title generation (heuristic + model upgrade)
- [x] Polling pattern for long-running model calls
- [x] Repo restructure: `backend/` + `ui/` cleanly separated
- [x] CI workflows + PR rules + CODEOWNERS + AI review action
- [x] User-message persistence on failure
- [x] Configurable model via `CHAT_MODEL` / `CHAT_MAX_TOKENS` / `CHAT_TEMPERATURE`
- [x] **Local-dev infra:** `docker-compose.yml` for Postgres + MinIO, `db/init.sql` schema bootstrap, `boto3` S3 facade replacing the Supabase-only storage path

### Files & tools
- [x] File upload via pending-attachment composer
- [x] S3-compatible file storage (Supabase Storage or MinIO via the same backend code)
- [x] Agent tools: `list_project_files`, `read_project_file`, `write_project_file`
- [x] Multi-format reading: text, PDF, DOCX, XLSX
- [x] Image reading via vision LLM
- [x] File viewer panel
- [x] Auto-refresh file chips after agent writes
- [x] Project sidebar shows uploaded files
- [x] Project create modal uploads files to the bucket

### Agent behavior
- [x] Tightened system prompt for tool-call discipline
- [x] Attached filenames embedded in LLM prompt
- [x] Intermediate assistant messages filtered from chat view

### GitHub (Phase 1 of 4)
- [x] Tables: `github_credentials`, `github_owner/repo/branch` on `sessions`
- [x] Endpoints: `/github/status`, `POST /github/token`, `DELETE /github/token`
- [x] UI: GitHub item in user menu → PAT management modal
- [x] PAT verification against GitHub `/user`

### Docs + skills
- [x] `architecture.md` with Mermaid diagram of the current system
- [x] `.claude/skills/update-readme/` — keeps README in sync with code
- [x] `.claude/skills/update-architecture/` — keeps `architecture.md` + diagram in sync

## 🔄 In progress

- Auto-maintain `architecture.md` + `report.md` on project file changes (system prompt is live; behavior still needs reinforcement on small models)
- README sync after the docker-compose / MinIO landing (run `/update-readme`)

---

# 🚀 Codex platform — phased plan

A staged path to turn the current chat agent into a Codex-like coding platform. Phases are roughly sequential but Phase 1 is a hard prerequisite for everything that follows — nothing else is safe without sandboxing.

> **Build philosophy:** don't break existing chat. Every new capability lives behind a new entity (a "task") or a new tool registered alongside the existing ones. The chat UI keeps working unchanged.

---

## Phase 1 — Sandboxed code execution

**Goal:** the agent can run arbitrary shell commands inside an isolated container with CPU/RAM/network/time limits.

**Why first:** every later phase (git ops, tests, PR creation, review) depends on it. Without sandboxing, a `shell` tool is a remote code execution gift to anyone who can sign up.

### Deliverables

- [ ] **Decision:** sandbox technology — Docker-in-Docker (DinD) vs Firecracker / gVisor / Kata. Default: DinD on a dedicated host. Firecracker only if multi-tenant untrusted.
- [ ] `sandbox/` service: thin Go/Python wrapper around the chosen runtime exposing `POST /workspaces`, `POST /workspaces/{id}/exec`, `DELETE /workspaces/{id}`. Streams stdout/stderr on exec.
- [ ] `workspaces` table: `{id, user_id, task_id?, session_id?, container_id, image, status, cpu_limit, mem_limit_mb, created_at, last_used_at, expires_at}`.
- [ ] Container base image: Python 3.13, Node 20, git, common build tools, pre-warmed package caches. One image per repo language family; start with a single "kitchen sink" image.
- [ ] Backend wrapper `backend/sandbox_client.py`: typed Python client for the sandbox service. Handles container lifecycle, exec, file copy in/out.
- [ ] New agent tool `shell_tool.py`: `run_shell(cmd, cwd=".", timeout=60)` → executes in the caller's workspace, returns `{stdout, stderr, exit_code, duration_ms}`. Streams events to the event bus (see Phase 4).
- [ ] Per-user quotas: max concurrent workspaces, max workspace lifetime, max exec time per call.
- [ ] Garbage collector: cron-ish task that tears down workspaces past `expires_at`.

### Changes to existing code

- `backend/api.py` — agent runnable config gains `workspace_id` alongside `user_id`/`session_id`. New endpoints `POST /workspaces` (create per task) and `DELETE /workspaces/{id}`.
- `backend/Tools/__init__.py` — register `shell_tool`.
- `docker-compose.yml` — add the `sandbox` service.

### Open decisions

- **Network access from sandbox?** Default deny; allowlist `pypi.org`, `registry.npmjs.org`, `github.com`. Else `npm install` can't work.
- **Volume model?** Per-workspace named volume (persistent across exec calls within the workspace lifetime; destroyed when workspace dies).
- **Image-build pipeline?** Manual `docker build` for now; later a CI job.

### Acceptance

The agent can be asked "run `ls -la` in a fresh workspace" via a chat message, sees the real output, and the workspace tears down 30 min later. Quotas are enforced.

### Estimate

~2–3 weeks for one developer including hardening.

---

## Phase 2 — Real git workflow

**Goal:** a task can clone a GitHub repo into a workspace, branch, edit, commit, push.

**Why now:** with Phase 1's sandbox + the user's existing PAT, this is just wiring. Unlocks every "do something to my code" use case.

### Deliverables

- [ ] On workspace creation for a repo-linked session: `git clone https://<token>@github.com/<owner>/<repo>.git /workspace`. PAT pulled from `github_credentials`, scoped to least-privilege.
- [ ] Tools: `git_status`, `git_diff`, `git_branch(name)`, `git_commit(message)`, `git_push(branch)`. Each is a thin wrapper over `run_shell` but emits a structured event (so the audit log shows "agent created branch X" not "agent ran `git checkout -b X`").
- [ ] Dual-backend file tools: `read_project_file` / `write_project_file` / `list_project_files` check whether a workspace is attached and, if so, read/write the workspace filesystem instead of S3. S3 stays for "loose" files (uploaded PDFs, attachments).
- [ ] `repo_links` becomes load-bearing — when a session has `github_owner/repo`, task workspaces auto-clone it; otherwise an empty workspace is created.
- [ ] PAT redaction in all logs and event streams — never let a token leak into chat history.

### Changes to existing code

- `backend/Tools/_paths.py` — add `get_workspace(config) -> Optional[str]` resolver.
- `backend/Tools/read_file_tool.py`, `write_file_tool.py`, `list_files_tool.py` — branch on workspace presence.
- `backend/api.py` — `_seed_project_files` writes architecture.md / report.md into the workspace (committed as the initial commit) when the project is repo-linked.

### Open decisions

- **Branch naming convention** for agent commits? Default: `agent/<task-id-short>`.
- **Commit author?** `<user-email> via Agent <agent@…>` so PR history is honest.
- **Squash on push, or push every commit?** Squash by default; preserve the granular commits as event-log entries.

### Acceptance

User can say in chat "fix the typo in README", agent clones, edits, commits, pushes a branch. `git log` on GitHub shows the commit with a sensible message.

### Estimate

~1–2 weeks.

---

## Phase 3 — Task abstraction + autonomous loop

**Goal:** "Do X" runs the agent autonomously (think → act → observe → repeat) until it finishes or hits a step limit. This is what makes Codex *Codex* instead of a chatbot.

### Deliverables

- [ ] `tasks` table: `{id, session_id, user_id, prompt, status (queued/running/needs_approval/done/failed), workspace_id, branch, step_count, max_steps, created_at, started_at, finished_at, result_summary}`.
- [ ] `task_events` table (or reuse `messages` with `kind="task_event"`): append-only log of every step (LLM reply, tool call, tool result, error, status change).
- [ ] Endpoints: `POST /tasks` (queue), `GET /tasks/{id}` (status), `POST /tasks/{id}/cancel`, `GET /tasks` (list mine).
- [ ] Task runner — async worker that:
  - Provisions a workspace (Phase 1)
  - Clones the repo if linked (Phase 2)
  - Runs the agent in a loop with `max_steps` budget
  - Records every step into `task_events`
  - On `task_complete` tool call, sets status `done` and writes a `result_summary`
- [ ] New agent tools: `task_complete(summary)`, `task_failed(reason)`. These are how the agent signals the loop to stop.
- [ ] Planner-executor split (optional but cleaner): one LLM call plans steps, a second loop executes them. Reduces flailing on small models.
- [ ] UI: "Start task" composer (separate from chat input) and a "Tasks" tab listing past + active runs.

### Changes to existing code

- `backend/api.py` — `lifespan()` starts the task worker (asyncio pool to begin with; swap to RQ in Phase 6 when parallel scaling matters).
- System prompts split: one for "chat assistant" (current), one for "task agent" (terser, action-oriented, knows the workspace exists).

### Open decisions

- **Step budget default?** Start at 50. Charge against the user's quota.
- **In-process worker vs Redis-backed queue?** In-process for Phase 3 (simpler); migrate to RQ in Phase 6.
- **Reuse the chat agent's checkpointer for tasks?** Yes — `thread_id = f"task:{task_id}"`, keeps resume/replay free.

### Acceptance

User types a task; the task runs to completion or step-limit; the events log shows every action; status transitions are visible.

### Estimate

~2 weeks.

---

## Phase 4 — Streaming, approvals, diff view

**Goal:** the user can *watch* the task as it runs, approve risky actions, and see the diff before it ships.

### Deliverables

- [ ] Event stream endpoint: `GET /tasks/{id}/events/stream` (Server-Sent Events). Subscribers get every new `task_event` row in real time.
- [ ] Approval gates — agent tool `request_approval(action, rationale)` sets task status to `needs_approval` and emits an approval event. `POST /tasks/{id}/approve` or `/deny` resumes the loop.
- [ ] Approval policy: configurable list of always-requires-approval actions — `git push`, anything writing outside the workspace, `pip install` of an unpinned package, network requests to non-allowlisted domains.
- [ ] Task detail page (UI) — three-pane layout:
  - **Left:** live event log (tool calls, shell output, LLM thoughts collapsible)
  - **Middle:** current working diff (`git diff` rendered with syntax highlighting)
  - **Right:** chat with the agent ("look at this commit also" → agent picks it up next step)
- [ ] Cancel button — sends SIGTERM to the running shell command, marks task `cancelled`.
- [ ] Replay mode — given a finished task, scrub through the event log step-by-step.

### Changes to existing code

- Keep `/history` polling for plain chat sessions (it works, no need to break it).
- `ui/lib/` — new SSE client utility (auth header via query param since EventSource doesn't support headers).

### Open decisions

- **WebSocket vs SSE?** SSE — one-way, simpler, no extra deps. WS only if you later need client→server streaming.
- **Diff renderer?** `react-diff-view` or hand-rolled. `react-diff-view` is fine.

### Acceptance

A running task shows live output. A `git push` triggers an approval modal. The diff pane updates after every file write.

### Estimate

~2–3 weeks (UI is the bulk).

---

## Phase 5 — PR creation + code review

**Goal:** tasks ship their work as PRs; the agent can also be pointed at an existing PR to review it.

### Deliverables

- [ ] `create_pr_tool` — at task end (or on demand), agent runs `git push`, then calls GitHub API to open a PR. Title + body generated from the task's event log (which files changed, what tests ran, summary).
- [ ] New task type `kind="review"` — input is a PR URL, agent clones, checks out the PR branch, reads the diff file-by-file, posts inline review comments via the GitHub API.
- [ ] Review system prompt — separate from the build prompt: emphasises asking "what could break", "what's untested", "what's inconsistent with the surrounding code".
- [ ] UI entry points alongside chat: "Start build task" (current), "Review PR…" (paste URL).
- [ ] Backlinks: a task that opened a PR shows the PR URL in its result summary; the PR has a comment linking back to the task replay.

### Changes to existing code

- `backend/github_client.py` — gains `create_pr`, `post_pr_review_comment`, `get_pr_diff` helpers.
- Reuse Phase 4's event-log → summary generation for PR descriptions.

### Open decisions

- **Auto-merge?** No, never. Codex doesn't either. PR opens; human merges.
- **Branch protection conflicts?** Detect and surface them as a task error, don't try to override.

### Estimate

~2 weeks.

---

## Phase 6 — Parallel agents + queue + quotas

**Goal:** N tasks at once across multiple users, with sane scheduling and cost ceilings.

### Deliverables

- [ ] Job queue (Redis + RQ or dramatiq). Replace Phase 3's in-process asyncio runner.
- [ ] Worker pool sized to sandbox capacity (e.g. 8 workspaces on a 32-core host).
- [ ] Per-user concurrency limits (`max_concurrent_tasks`), per-user daily quotas (`max_tasks_per_day`, `max_llm_tokens_per_day`).
- [ ] Sandbox eviction: idle > N minutes → tear down; reuse warm workspaces for the same repo when possible (cache `git clone` time).
- [ ] Cost accounting: every LLM call records `model + input_tokens + output_tokens + cost_estimate` in `task_events`; surface per-task and per-day totals in UI.
- [ ] Dashboard: "active tasks" tray showing all running tasks across the user, with status + cost-so-far.

### Changes to existing code

- `docker-compose.yml` — add Redis service.
- `backend/` — extract the task runner from `lifespan()` into a standalone `worker.py` entry point. Backend API and worker can scale independently.

### Open decisions

- **Workspace warm pool?** Pre-warm 1–2 idle workspaces per common base image to cut task start time from ~30s to ~2s.
- **Failover?** A worker crash mid-task should mark the task `failed` (via a heartbeat row that goes stale). Resume-from-checkpoint is future work.

### Estimate

~2 weeks.

---

## Phase 7 — Customization, multi-model, observability

**Goal:** make it usable for real repos by giving the agent repo-specific context, the right model for each job, and a window into what it's actually seeing.

### Deliverables

- [ ] **`AGENTS.md` parsing** — on workspace init, read repo-root `AGENTS.md` / `CLAUDE.md` and inject into the system prompt. Per-directory `AGENTS.md` supported via nearest-ancestor lookup.
- [ ] **Model role split** — `PLANNER_MODEL`, `EXECUTOR_MODEL`, `REVIEWER_MODEL` env vars (with sensible defaults). Planner = smart+slow, executor = fast, reviewer = smart. Each used in the right place.
- [ ] **Generic MCP loader** — list MCP servers in env (`MCP_SERVERS=shell,github,filesystem,notion,…`), spawn each at backend startup, auto-discover their tools, adapt MCP tool schemas → LangChain tools, register dynamically. Per-user enable/disable in settings.
- [ ] **Token-reduction via summarization** — LangGraph `SummarizationNode` for long conversations; show active summary in the context viewer.
- [ ] **Context window viewer** — floating button in the web UI → side panel showing system prompt, full message history, files in scope, per-message tokens, % of model limit used.
- [ ] **In-browser logs viewer** — searchable per-task log of every shell command, tool call, and LLM exchange. Replaces "open a terminal to see what happened" since we're web-only.

### Deferred (web-compatible but lower priority)

- Public REST API + API tokens, so users can drive tasks programmatically from outside the browser (curl, scripts, GitHub Actions). Same endpoints, just bearer-token auth instead of JWT.

### Estimate

~3 weeks total (each item is small to medium).

---

## Cross-cutting concerns to commit to early

These shape architecture; deciding late is expensive. Tracked here as standing decisions to revisit each phase.

| Decision | Options considered | Current default |
| --- | --- | --- |
| Sandbox isolation | DinD / Firecracker / gVisor / Kata | **DinD** — single-host, single-trust-domain users |
| Background workers | Asyncio in-process / RQ+Redis / Celery / Temporal | **Asyncio → RQ+Redis from Phase 6** |
| Event transport | SSE / WebSocket / Postgres LISTEN+poll | **SSE** — one-way is enough |
| Auth model | Keep Supabase / move to local OIDC / add OAuth (GitHub) | **Keep Supabase**, add GitHub OAuth as a stretch goal |
| Cost tracking | Track per task from day 1 / add later | **Track from day 1** (Phase 3 onward) — retrofit is painful |
| Secrets in workspaces | Mounted file / env var injection / OIDC token exchange | **Env var injection at workspace start** for the PAT only; redact in logs |

---

## Recommended first deliverable (before committing to Phase 1)

A **one-week vertical slice** that proves the architecture end-to-end on the simplest possible task:

> "Clone <repo>, run the test suite, report failures."

Hard-code everything that's not essential:
- One sandboxed container (no quotas)
- Hard-coded repo URL (no UI)
- Single linear agent loop (no streaming, no approvals)
- Poll for "done" instead of SSE

If that works in a week, the rest is incremental. If it doesn't, you've learned the real obstacles before sinking weeks into the proper Phase 1.

---

## 🎯 Stretch goals (not yet committed)

- RAG over uploaded files (pgvector embeddings)
- Multi-modal in main chat model (when a stable free-tier vision model exists)
- OAuth "Sign in with GitHub" replacing PAT
- Token encryption at rest (pgcrypto / Vault) for `github_credentials.token`
- Diff view for chat-mode file edits (Phase 4 covers it for tasks)
- Shareable read-only project links
- Folder-structure-preserving uploads
- VS Code extension

## Out of scope

This is a **web application**. Anything that doesn't fit in a browser tab is out:

- Native desktop apps (Electron / Tauri wrappers)
- Browser extensions and VS Code / JetBrains plugins
- Editing the user's local filesystem (browsers can't, and we won't ship a wrapper that can)
- Native CLI shipped as a binary
- Multi-tenant team workspaces (each user is independent)
- Real-time collaboration on a single chat or task

---

## Notes for future contributors

- Tools live in [backend/Tools/](backend/Tools/) — one file per tool, registered in [\_\_init\_\_.py](backend/Tools/__init__.py)
- File operations all go through [backend/storage.py](backend/storage.py) — never touch disk directly
- LangGraph thread ID is `{user_id}:{session_id}` for chat and `task:{task_id}` for tasks — never change either format without a checkpoint migration
- Frontend file paths follow Next.js App Router; the workspace UI lives in [ui/app/app/page.tsx](ui/app/app/page.tsx)
- The current architecture diagram is in [architecture.md](architecture.md); keep it in sync via `/update-architecture` when phases land
- Setup + run commands live in [README.md](README.md); keep in sync via `/update-readme`
- See [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md) for branching/PR rules
