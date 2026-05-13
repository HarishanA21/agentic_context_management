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

### GitHub
- [x] Tables: `github_credentials`, `github_owner/repo/branch` on `sessions`
- [x] Endpoints: `/github/status`, `POST /github/token`, `DELETE /github/token`
- [x] UI: GitHub item in user menu → PAT management modal
- [x] PAT verification against GitHub `/user`
- [x] **`POST /github/repo`** — server-side new-repo creation; scope verification with `repo` requirement; fine-grained PATs rejected with actionable error
- [x] **Project-creation flow with GitHub linkage** — `POST /sessions` accepts `github_mode` (`none` / `new_repo` / `link_existing`); UI has a 3-segment selector with inline "Connect PAT" prompt when missing; persists via existing `github_owner/repo/branch` columns

### Sandboxed workspaces (Phase 1)
- [x] **Sandbox image** — [sandbox/Dockerfile](sandbox/Dockerfile) with Python 3.13, Node 20, git, build tools; non-root `agent` user; `acm-workspace:latest`
- [x] **Pluggable backend** — [backend/sandbox_client.py](backend/sandbox_client.py): `SandboxBackend` ABC + `DockerBackend` (host-socket) + `E2BBackend` (Firecracker microVM); `SANDBOX_BACKEND` env-var-selected; PAT redaction
- [x] **`workspaces` table** — `id, user_id, session_id, backend, backend_ref, status, created_at, last_used_at, expires_at`; indexes for GC scan, idle-pause, per-user cap, session lookup
- [x] **Workspace endpoints** — `POST/GET/DELETE /workspaces`; lazy-create + auto-resume + drift reconciliation between DB and backend
- [x] **GC loop + quotas** — 5-min cadence, pause idle >15 min, destroy past `expires_at`; 3 concurrent active per user
- [x] **`run_shell` agent tool** — full bash inside the workspace, output formatted for the LLM with `(exit N, M ms)` headers, 16 KB stream caps, deadline-enforced timeout
- [x] **`/chat` workspace wiring** — project sessions lazy-create a workspace and inject `workspace_ref` into the agent's runnable config
- [x] **Workspace bootstrap** — fresh workspaces auto-clone the linked GitHub repo (PAT embedded then stripped from remote) or `git init` an empty repo + initial commit so HEAD exists for rollback; shell-injection defenses

### Docs + skills
- [x] `architecture.md` with Mermaid diagram of the current system
- [x] `.claude/skills/update-readme/` — keeps README in sync with code
- [x] `.claude/skills/update-architecture/` — keeps `architecture.md` + diagram in sync

## 🔄 In progress

- **Phase 2A — git workflow** — Steps 2.1–2.3 + 2.5 done. **Step 2.4** (git tools — `git_status`/`git_diff`/`git_branch`/`git_log`) is the remaining 2A item; lower priority now that auto-commit is in.
- **Phase 2B — rollback + history** — Steps 2.6, 2.7, 2.8, 2.10 all done. Rollback is fully usable end-to-end: edit → auto-commit → see in history → undo → file reverts. **Step 2.9** (push flow) is the only 2B item remaining, deferred per user direction (MCPs will cover it).
- **Phase 1 — Step 1.8** — E2B-side smoke test pending until a real `E2B_API_KEY` is plugged in. Docker side fully validated. E2BBackend code updated to use `Sandbox.create()` classmethod (2026 SDK shape) — untested but should now match the installed signature.
- **Push to GitHub (Step 2.9)** — deferred (user choice — will add via MCP later).
- Auto-maintain `architecture.md` + `report.md` on project file changes (system prompt is live; behavior still needs reinforcement on small models).

---

# 🚀 Codex platform — phased plan

A staged path to turn the current chat agent into a Codex-like coding platform. Phases are roughly sequential but Phase 1 is a hard prerequisite for everything that follows — nothing else is safe without sandboxing.

> **Build philosophy:** don't break existing chat. Every new capability lives behind a new entity (a "task") or a new tool registered alongside the existing ones. The chat UI keeps working unchanged.

---

## Phase 1 — Sandboxed code execution (pluggable backend)

**Goal:** the agent can run arbitrary shell commands inside an isolated workspace with CPU/RAM/network/time limits. Two backends behind one interface — Docker socket for local solo dev (fast, free, unsafe), E2B microVMs for multi-user production (safe, costs money beyond free tier).

**Why first:** every later phase (git ops, tests, PR creation, review) depends on it. Without sandboxing, a `shell` tool is a remote code execution gift to anyone who can sign up.

### Backend strategy: pluggable, env-var-selected

`SANDBOX_BACKEND=docker` (default in dev) | `SANDBOX_BACKEND=e2b` (production multi-user).

`backend/sandbox_client.py` exposes the same surface either way: `create`, `exec`, `read_file`, `write_file`, `pause`, `resume`, `destroy`. Every other module (`shell_tool`, file tools, GC loop, git tools in Phase 2) talks to the abstraction — swapping backends is a one-line config change, not a code rewrite.

> **🚨 Safety boundary:** the Docker-socket backend is for **local-host, single-user development only**. A container escape with `/var/run/docker.sock` mounted = root on the host. Before letting any second person hit this app — even a friend testing it — flip `SANDBOX_BACKEND=e2b`. This is the hard line between "dev tool I use" and "service other people touch".

### Backend A — Docker (local dev, default now)

Researched 2026-05-13. Plain Docker via the host socket is the fastest, cheapest path while it's just you on localhost.

- **Why:** zero new infra (Docker already installed), instant feedback loop (no API roundtrips), free, easy to inspect (`docker ps`, `docker logs`, `docker exec`).
- **Image:** `acm-workspace:latest` — Python 3.13, Node 20, git, common build tools. Built by `sandbox/Dockerfile`.
- **Trade-offs:**
  - **Unsafe for multi-tenant.** Container escape = host root because we mount the Docker socket. Solo-dev only.
  - No native pause/resume — we just stop/start containers (state survives on volumes, memory does not).
  - No outbound network gating by default; locked down per-image if/when we need it.

### Backend B — E2B (multi-user production, swap-in)

Researched 2026-05-13. Multi-user threat model rules out plain Docker / Docker-socket / Sysbox (all share host kernel). Real isolation requires microVMs (Firecracker/Kata) or user-space syscall interception (gVisor).

- **Chosen runtime: E2B Hobby → Pro** ([e2b.dev](https://e2b.dev/pricing)).
  - **Why:** Firecracker microVM isolation (same tech as AWS Lambda), $100 free credit with no credit card, 20 concurrent sandboxes free, pause/resume preserves filesystem + memory state (matches our 24h-TTL-after-last-use exactly), Python SDK drops straight into the FastAPI backend, full Linux with git/python/node, ~150ms cold start.
  - **Free-tier cost projection:** at 10 users × 10 tasks/day × 2 min ≈ $10/mo of credit → ~10 months free. At 100 users × 50 tasks/day, ~$650/mo on E2B Pro.
  - **Trade-offs:** 1-hour single-session cap on Hobby (chain via pause/resume); no built-in egress allowlisting (wire ourselves later).
- **Scale-up path** when E2B costs cross ~$300/mo: migrate to **self-hosted Firecracker on Hetzner CCX33** (~€30/mo for ~30 concurrent microVMs). Same `sandbox_client.py` interface, third backend implementation.
- **Rejected:** Modal (3× pricier sandbox tier + 2–5s cold starts), Daytona (cheaper but shared-kernel isolation), Fly Machines (no free tier as of 2024), Cloudflare Containers (Worker-bound model + $5/mo floor).

### Deliverables

- [x] **Step 1.1** — `sandbox/Dockerfile` and `sandbox/build.sh` for the local workspace image (Python 3.13 + Node 20 + git + build tools), tagged `acm-workspace:latest`. Plus E2B account creation and SDK install for backend B (`pip install e2b`); `E2B_API_KEY` env var stubbed.
- [x] **Step 1.2** — `backend/sandbox_client.py` — abstract base class `SandboxBackend` with `create`, `exec`, `read_file`, `write_file`, `pause`, `resume`, `destroy`. Two implementations:
  - `DockerBackend` — uses the Docker SDK against the host socket. Image: `acm-workspace:latest`. `pause`/`resume` map to `docker stop`/`docker start`.
  - `E2BBackend` — uses the E2B Python SDK. Native pause/resume preserves filesystem + memory.
  - A `get_backend()` factory returns the right one based on `SANDBOX_BACKEND` env var. Secrets redacted in all logs from both backends.
- [x] **Step 1.3** — `workspaces` table in `db/init.sql`: `{id, user_id, session_id, backend (text), backend_ref (text), status (running|paused|destroyed), created_at, last_used_at, expires_at}`. `backend_ref` stores the Docker container ID or E2B sandbox ID — interpretation depends on `backend`.
- [x] **Step 1.4** — Endpoints `POST /workspaces`, `GET /workspaces/{id}`, `DELETE /workspaces/{id}`. Every call bumps `last_used_at` and sets `expires_at = now() + 24h`. `exec` auto-resumes if paused.
- [x] **Step 1.5** — GC loop in `lifespan()` (5-min cadence): pause workspaces idle >15 min (frees compute — matters more on E2B than Docker), destroy ones past `expires_at`. Per-user cap: 3 concurrent active sandboxes (429 above that).
- [x] **Step 1.6** — `shell_tool` (`run_shell(cmd, cwd=".", timeout=60)`). Resolves `workspace_id` from agent config, calls `sandbox_client.exec`, returns stringified output. Register in `Tools/__init__.py`.
- [x] **Step 1.7** — Wire to chat: when `/chat` runs on a `kind="project"` session, lazy-create a workspace and pass `workspace_id` into the agent's runnable config alongside `user_id`/`session_id`.
- [ ] **Step 1.8** — Smoke test against both backends: same script (create → exec → destroy) runs green with `SANDBOX_BACKEND=docker` and `SANDBOX_BACKEND=e2b`. Proves the abstraction holds. _(Docker side green via [sandbox/smoke_test.py](sandbox/smoke_test.py) and [sandbox/test_chat_flow.py](sandbox/test_chat_flow.py); E2B side pending a real API key.)_

### Changes to existing code

- `backend/api.py` — agent runnable config gains `workspace_id` alongside `user_id`/`session_id`. New `/workspaces/*` endpoints. `lifespan()` starts the GC loop.
- `backend/Tools/__init__.py` — register `shell_tool`.
- `backend/requirements.txt` — add `docker` (Python SDK) and `e2b`.
- `backend/.env.example` — add `SANDBOX_BACKEND=docker` (default), `E2B_API_KEY=` (optional, required only when backend=e2b), `WORKSPACE_TTL_HOURS=24`, `WORKSPACE_IDLE_PAUSE_MIN=15`, `WORKSPACE_MAX_PER_USER=3`.

### Open decisions (resolved)

| Decision | Resolution |
| --- | --- |
| Sandbox tech | **Pluggable**: Docker socket for solo local dev, E2B (Firecracker microVM) for any multi-user deployment. Same `SandboxBackend` interface. |
| Default backend | `SANDBOX_BACKEND=docker` |
| Multi-user trigger | Flip to `SANDBOX_BACKEND=e2b` before anyone besides the developer hits the URL. Non-negotiable. |
| TTL | 24h after last use; pause when idle >15 min |
| Concurrency cap | 3 active sandboxes per user |
| Network egress | Default allow everything; tighten when we deploy multi-user |
| Image | `acm-workspace:latest` (local Dockerfile) for Docker backend; E2B default `base` for E2B backend |

### Acceptance

With `SANDBOX_BACKEND=docker`: the agent in a project session can be asked "run `ls -la /` in the workspace" via chat and sees real output. Workspaces past 24h get destroyed within 5 minutes. A 4th concurrent workspace per user returns 429.

With `SANDBOX_BACKEND=e2b`: same smoke test passes against an E2B microVM. No code changes required to swap.

### Estimate

~4–6 days. Dockerfile + Docker backend is fast (~1 day); E2B backend is ~1 day on top; the rest (table, endpoints, GC, tool, wiring) is the same regardless of backend.

---

## Phase 2 — Real git workflow + hybrid rollback

**Goal:** every workspace is a real git repo from minute one (so rollback works for everyone). GitHub linkage is optional; when present, the user explicitly approves each push.

### Rollback model: hybrid (local-always + GitHub-on-permission)

- **Local commits are silent and automatic.** Every agent file write triggers an auto-commit in the workspace's local git repo, regardless of whether GitHub is connected. Rollback works for everyone, every time.
- **GitHub pushes are opt-in and per-permission.** When a session has a linked repo, the user gets a prompt at natural state-points (end of chat turn, end of task, manual "save my work" click) listing the unpushed commits — they confirm to push or defer to push later in a batch.
- **GitHub repo creation is offered during project creation.** Three choices: create a new repo for me (we call GitHub API; needs `repo` PAT scope), link an existing repo, or skip entirely.

### Phase 2A — Git workflow

- [x] **Step 2.1** — `backend/github_client.py`: add `verify_token_scopes(token) → [scopes]` and `create_repo(token, name, private=True) → {owner, repo, default_branch}`. New endpoint `POST /github/repo`.
- [x] **Step 2.2** — Project creation flow. Extend `POST /sessions` with `github_mode: "none" | "new_repo" | "link_existing"`. UI: project modal gains a "Save history to GitHub?" step with three buttons. For `new_repo`, verify the PAT has `repo` scope first; if missing, prompt inline for a re-paste (don't kick out to the user-menu modal). Persist the link via existing `github_owner/repo/branch` columns.
- [x] **Step 2.3** — Auto-init/clone on workspace start. For repo-linked sessions: `git clone https://<token>@github.com/<owner>/<repo>.git /workspace`. For unlinked: `git init /workspace`. Both paths leave the workspace as a valid git repo from minute one. Configure committer identity per session.
- [ ] **Step 2.4** — Git tools in `backend/Tools/git_tools.py`: `git_status`, `git_diff`, `git_branch`, `git_log`. Thin wrappers over `run_shell` that parse output into structured dicts (easier for the agent to reason about than raw text).
- [x] **Step 2.5** — Dual-backend file tools. `get_workspace_ref(config)` in [backend/Tools/_paths.py](backend/Tools/_paths.py); workspace-aware [list_files_tool.py](backend/Tools/list_files_tool.py) / [read_file_tool.py](backend/Tools/read_file_tool.py) / [write_file_tool.py](backend/Tools/write_file_tool.py). Reads try workspace then fall through to S3; writes go to workspace (committed) when a workspace is attached, S3 otherwise. Listing shows both surfaces with clear separation.

### Phase 2B — Rollback + history UI

- [x] **Step 2.6** — Auto-commit on every workspace file write. After `write_project_file` succeeds in a workspace, runs `git add + git commit` via an env-var-passed inline script (`$ACM_FILE`). Verb derived from `git status --porcelain` ("created" for `A`, "updated" for `M`, skip on no-change). Best-effort: commit failure is logged but write still reported as successful. Surfaces `(committed as Agent: updated hello.py (a1b2c3d))` in the tool response.
- [x] **Step 2.7** — `workspace_commits` table (`id, workspace_id, session_id, user_id, sha, message, pushed_at, reverted_at, created_at`; `UNIQUE (workspace_id, sha)`). End-of-turn `_sync_workspace_commits` in `/chat` mirrors the workspace's `git log` into the table (idempotent via `ON CONFLICT DO NOTHING`, oldest-first insertion so newer commits get higher serial ids). `GET /sessions/{id}/history` returns the timeline newest-first with `status` of `local | pushed | reverted`.
- [x] **Step 2.8** — Undo endpoint `POST /sessions/{id}/history/{commit_id}/revert`. Runs `git revert --no-edit <sha>` inside the workspace; aborts cleanly on conflict and returns 409. Stamps `reverted_at` on the original row; the new revert commit appears via the next sync. Refuses to revert an already-reverted commit (409) or a workspace that's been destroyed (410).
- [ ] **Step 2.9** — Push flow with permission gate. `GET /sessions/{id}/unpushed-commits` returns rows where `pushed_at IS NULL AND reverted_at IS NULL`. `POST /sessions/{id}/push` runs `git push origin <branch>`, stamps `pushed_at` on the included rows. End-of-turn auto-prompt: when chat finishes and unpushed count > 0, emit a UI event ("N changes ready to push to <repo>") with [Push now] / [Later] buttons. **Defer to user always** — never push without explicit confirmation.
- [x] **Step 2.10** — History panel UI. Floating button on the right edge (project sessions only) opens a slide-in panel that lists commits newest-first with short SHA, timestamp, message, and a status badge (local / pushed / reverted). Each row has an `Undo` button that opens a confirm modal and then POSTs to the revert endpoint; the panel + file sidebar refresh on success. Already-reverted commits and the initial commit are disabled. Push buttons / "Push all" deferred along with Step 2.9.

### Changes to existing code

- `backend/Tools/_paths.py` — add `get_workspace_id(config)`.
- `backend/Tools/read_file_tool.py`, `write_file_tool.py`, `list_files_tool.py` — branch on workspace presence.
- `backend/api.py` — `_seed_project_files` writes architecture.md / report.md into the workspace as the initial commit when repo-linked.
- `backend/github_client.py` — `verify_token_scopes`, `create_repo`.
- `db/init.sql` — add `workspace_commits` table.

### Open decisions (resolved)

| Decision | Resolution |
| --- | --- |
| Rollback model | **Hybrid**: local commits always (silent, automatic), GitHub pushes opt-in and per-permission |
| Push trigger | At end of chat turn / task / state-point → prompt user → push or defer |
| New-repo-on-creation | **Offered** during project creation (needs `repo` PAT scope; inline re-paste if missing) |
| Commit message style | **One-line template** (e.g. `Agent: updated <file>`); no extra LLM call |
| Branch for agent commits | `main` for new repos, default branch for linked repos. Branch-per-task comes in Phase 5 (PR creation). |
| PAT scope handling | Verify before offering `new_repo`; prompt inline for re-paste if missing `repo` scope |

### Acceptance

1. User creates a project with "create new repo" → a private repo appears on their GitHub.
2. User asks in chat to "create a README"; the agent writes it; the file appears in the workspace.
3. The History panel shows one entry: "Agent: updated README.md — local".
4. The end-of-turn prompt asks "1 change ready to push to <repo>"; the user clicks Push; the commit appears on GitHub.
5. The user clicks Undo on a different commit; the file reverts in the workspace; a new "Undo: …" entry appears.

### Estimate

~1–2 weeks across Phase 2A + 2B.

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

**Path A (prompt-based, shipped now):**

- [x] **Rich tool-message cards** in chat — `run_shell` as terminal card (parsed `$ cmd / exit N / M ms / stdout / stderr`); `write_project_file` as edit card with Created/Edited verb badge, byte count, and short SHA; `read_project_file` and `list_project_files` as compact cards; errors as red cards. Thinking indicator now labelled "Agent — thinking…".
- [x] **Auto / Confirm mode toggle** in the project header — `mode` column on sessions, `PATCH /sessions/{id}` endpoint, `/chat` prepends a confirmation-mode preamble to the user message when set. Prompt-based enforcement (the model is asked to ask first); not bulletproof, but covers the demo path.
- [x] **Diff expander** on file-edit cards — `View diff` button fetches `GET /sessions/{id}/commits/{sha}/diff` (which runs `git show --no-color` in the workspace) and renders a colored unified diff with `+N/-M` summary in the card header.

**Path B (hard interrupts):**

- [x] **SSE event stream** — `GET /sessions/{id}/threads/{tid}/stream?token=<jwt>` (JWT in query because EventSource can't send headers). [backend/event_bus.py](backend/event_bus.py) implements an in-memory per-key pub/sub; `_record_message` and `_sync_workspace_commits` publish on insert. UI's `EventSource` subscription replaces the mid-turn polling for *display*; the existing send-time polling stays only as a completion-detection backstop.
- [x] **Hard approval gates via LangGraph `interrupt()`** — `write_project_file` and `run_shell` call `interrupt(...)` before any side effect when `session_mode='confirm'`. `_pending_approval` inspects post-invoke state for pending interrupts and returns `{interrupted: true, approval}` to the UI; an SSE `approval_request` event flushes the card in real time. **`POST /chat/resume`** continues the run via `Command(resume={approved, reason})`. Chained interrupts are handled — multi-write turns surface one approval at a time.
- [x] **Approval UI card** — amber `ApprovalCard` renders inline in chat for the active pending interrupt. For `write_project_file` shows filename, byte count, and a content preview; for `run_shell` shows the cwd and the command. `[Approve]` / `[Deny]` buttons POST to `/chat/resume`.
- [x] **Approval policy** — `_ALWAYS_CONFIRM_PATTERNS` in [shell_tool.py](backend/Tools/shell_tool.py) escalates `git push`, `git reset --hard`, force-push, `sudo`, `rm -rf /`, redirects outside `/workspace`/`/tmp`/`/dev/null`, and `curl|wget` with write methods to a hard approval gate even in Auto mode. The pattern label is forwarded as `policy_reason` so the approval card can explain *why* it's asking.
- [x] **Task detail layout** — `ViewToggle` (Chat / Task) in the project header. Task view is a 3-col grid: **Activity** (tool messages + bare-tool-call stubs), **Working diff** (auto-fetched for the most recent commit SHA seen in messages), **Chat** (user + assistant prose only, with approval card + Stop button + composer). All three columns scroll independently and share the same SSE feed.
- [x] **Cancel button** — `POST /chat/cancel` runs `pkill -KILL -P 1` inside the session's workspace, killing any in-flight shell process. UI surfaces a `Stop` button while `sending` is true; toast confirms whether it actually killed processes. Doesn't cancel the upstream LLM call (no handle on the OpenRouter request) but the shell-kill is usually enough to abort the agent loop.
- [x] **Replay mode** — `ReplayControls` in the header. Enter to freeze rendering at `displayMessages = messages.slice(0, replayIdx)`; `◀ / ▶` step through; `Live` exits. Works in both Chat and Task views. While replaying, the typing indicator, approval card, and Stop button are hidden to make clear you're viewing history, not the live state.

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
| Sandbox isolation | Docker socket / DinD / gVisor / Kata / Firecracker (self-hosted) / E2B (managed) / Modal / Daytona | **Pluggable backend**: Docker socket for local solo dev (default), E2B (managed Firecracker) for any multi-user deploy. Same `SandboxBackend` interface — flip via `SANDBOX_BACKEND` env var. Self-hosted Firecracker on Hetzner is the scale-up target after ~$300/mo on E2B. |
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
