# FYP Agent — Project Roadmap

## Vision

A web-based agent that behaves like Claude / ChatGPT / Codex — running in the browser, supporting per-user authentication, project-scoped context, file uploads, GitHub integration, and terminal access. The goal is a working full-stack agent system that demonstrates real-world agentic patterns: tools, memory, persistent context, file I/O, version control, and self-maintenance.

## Stack

- **Frontend** — Next.js 14 (App Router) + Tailwind + TypeScript
- **Backend** — FastAPI + LangGraph (`create_agent`) + LangChain
- **Auth** — Supabase Auth (email/password, JWT verified via JWKS server-side)
- **Persistence** — Supabase Postgres (sessions, threads, messages, LangGraph checkpoints, GitHub credentials)
- **File storage** — Supabase Storage bucket (`project-files`), scoped by `user_id/session_id/`
- **Main chat model** — `meta-llama/llama-3.3-70b-instruct:free` via OpenRouter (configurable via `CHAT_MODEL` env var)
- **Vision model** — `meta-llama/llama-3.2-11b-vision-instruct:free` (image reads only)

## ✅ Done

### Core
- [x] Supabase Auth (signup, login, JWT-protected backend)
- [x] Persistent multi-project, multi-thread chat history (Postgres)
- [x] LangGraph agent with checkpointer (state survives restarts, per-user thread scoping)
- [x] Row Level Security (RLS) on all user data + storage bucket
- [x] Project / chat distinction in UI (kind = "project" with multiple chats, or "chat" with one)
- [x] Per-row sidebar menu on projects, chats, and **chats inside projects** (rename, delete, move to project, share)
- [x] Centered confirm + prompt modals (no more browser dialogs)
- [x] Right-side file viewer with line numbers
- [x] Markdown rendering (react-markdown + remark-gfm)
- [x] Auto-title generation (heuristic + model upgrade)
- [x] Polling pattern for long-running model calls (handles dev-proxy timeouts)
- [x] Repo restructure: `backend/` + `ui/` cleanly separated
- [x] CI workflows + PR rules + CODEOWNERS + AI review action
- [x] User-message persistence on failure — user's text + error message survive in DB if `/chat` crashes mid-turn
- [x] Configurable model via `CHAT_MODEL` / `CHAT_MAX_TOKENS` / `CHAT_TEMPERATURE` env vars

### Files & tools
- [x] File upload via pending-attachment composer (ChatGPT-style chips)
- [x] File storage in Supabase bucket (migrated from local disk)
- [x] Agent tools: `list_project_files`, `read_project_file`, `write_project_file`
- [x] Multi-format reading: text, PDF (pypdf), DOCX (python-docx), XLSX (openpyxl)
- [x] Image reading via vision LLM (vision model on OpenRouter)
- [x] File viewer panel (line-numbered code view)
- [x] Auto-refresh file chips after agent writes
- [x] **Project sidebar shows uploaded files under each project folder** with click-to-view
- [x] Project create modal actually uploads files to the bucket (was previously a stub)

### Agent behavior
- [x] Tightened system prompt — agent must call tools when promising file actions, not just narrate
- [x] Attached files include the filenames in the LLM prompt so the agent doesn't ask "what should I explain"
- [x] Filtered intermediate assistant messages (tool-call preambles) out of the chat view — only final answers render

### GitHub (Phase 1 of 4)
- [x] Database tables: `github_credentials`, `github_owner/repo/branch` on `sessions`
- [x] Backend: `/github/status`, `POST /github/token`, `DELETE /github/token`
- [x] UI: GitHub item in user menu → modal to paste/manage PAT
- [x] Verification: PAT is validated against GitHub `/user` before storing

## 🔄 In progress

### Auto-create default project files (architecture.md + report.md)
Building this next. See [§ 1 below](#1-auto-create-architecturemd--reportmd-on-project-creation).

## 📋 Roadmap (priority order)

### 1. Auto-create architecture.md + report.md on project creation
**Why now:** Foundation for the "agent maintains its own architecture" feature. Cheap to add. **Effort:** ~2 hrs.

- When a session is created with `kind="project"`, backend writes two starter files to the bucket:
  - `architecture.md` — placeholder template the agent maintains as the project evolves
  - `report.md` — running log; first line is "Project created"
- Frontend passes `kind: "project"` in `POST /sessions` body
- Files visible immediately in the sidebar FILES section under the new project

### 2. Auto-maintain architecture.md
**Why:** Makes the agent self-documenting. Builds on §1. **Effort:** ~3 hrs.

- System prompt addition (project sessions only): *"After every meaningful change to project files, re-read architecture.md, update it if the structure changed, and append a line to report.md describing what just happened."*
- Test on small projects first — the model might forget. Reinforce in tools if needed.
- Possibly add a `update_architecture(summary)` convenience tool that the model can call instead of opening + writing manually.

### 3. Finish GitHub integration (Phases 2-4)
**Why:** Phase 1 (PAT auth) is done; the actual file operations against GitHub are still missing. **Effort:** ~6 hrs.

- **Phase 2 — Read tools** (~2 hrs)
  - `connect_github_repo(owner, repo, branch)` — link this project to a repo via the UI's New Project modal (not via agent chat)
  - `read_github_file(path)` — read any file from the linked repo
  - `list_github_commits(limit)` — recent commits with messages, authors, dates
- **Phase 3 — Write tools** (~3 hrs)
  - `push_to_github(message)` — push all current project files as one commit
  - `revert_github_commit(sha)` — reset branch to that commit (destructive, force-push)
  - Conflict / branch-protection handling
- **Phase 4 — UI polish** (~1 hr)
  - Show linked repo in chat header ("📁 owner/repo · main")
  - Click to unlink / change branch
- **Architecture note:** When a project is linked to a GitHub repo, file tools should transparently route to GitHub (commit) instead of the bucket. A `file_backend.py` abstraction is needed; see corrected plan in chat history.

### 4. Context window viewer (round button)
**Why:** Lets you see exactly what the LLM sees on each turn — invaluable for debugging the later phases (summarization, architecture maintenance). **Effort:** ~3 hrs.

- Floating circular button (bottom-right of chat) — like Intercom widget but minimal
- Click → side panel showing:
  - System prompt
  - Full message history about to be sent to the model
  - Files currently in scope
  - Per-message token count + running total
  - Model's context window limit + percentage used
- Read-only — just observability
- Backend endpoint: `GET /sessions/{sid}/threads/{tid}/context` returning serialized state

### 5. Token-reduction via summarization
**Why:** Long conversations blow the context window. Without summarization, old turns get truncated arbitrarily. **Effort:** ~6 hrs.

- Use LangGraph's built-in summarization node (`SummarizationNode` or similar)
- Trigger condition: when messages > N or token count > X% of model limit
- Compressed summary replaces the oldest N-K messages
- UI toggle: "Show with summarization" vs "Show without" — lets you compare quality
- Show the active summary in the context window viewer (§4)

### 6. Terminal tool for agent
**Why:** Unlocks code execution, builds, tests — true Codex-like behavior. **Effort:** ~5 hrs. **⚠ security-sensitive.**

- Add tool: `run_terminal(command: str, timeout_sec: int = 30)`
- **Sandbox decisions needed:**
  - Allowlist of commands (e.g. `git`, `npm`, `python`, `node`, `pytest`) or full shell?
  - Working directory: scoped to project's upload dir? A fresh tmp dir per call?
  - Network access?
- Best path: **use MCP shell server** (`@modelcontextprotocol/server-shell`) — it already implements sandboxing
- Without sandbox, this is dangerous on a shared backend — at minimum, deny `rm`, `curl`, network commands

### 7. MCP server integration (generic loader)
**Why:** Once you have one MCP server (terminal), it's worth building a generic loader so you can add more (Notion, Linear, Slack) by config. **Effort:** ~8 hrs.

- Architecture: a list of MCP servers in `backend/.env` (e.g. `MCP_SERVERS=shell,github,filesystem`)
- On backend startup, spawn each as a subprocess
- Discover their tools via the MCP handshake
- Adapt MCP tool schemas → LangChain tools dynamically
- Surface them all in `all_tools` registered with the agent
- Per-user disable/enable in settings

## 🎯 Stretch goals (not committed)

- **RAG over uploaded files** — pgvector embeddings, semantic recall instead of re-parsing every turn
- **Multi-modal in main chat model** — switch when a stable vision-capable model is on free tier
- **OAuth GitHub flow** — replace PAT with proper "Sign in with GitHub" button
- **Token encryption at rest** — Supabase Vault or pgcrypto for the `github_credentials.token` column
- **Streaming responses** — Server-Sent Events instead of long-poll
- **Diff view for file changes** — show before/after when the agent edits a file
- **Branch-aware GitHub tools** — work on a non-default branch, push to PRs
- **Shareable read-only project links** — public view, no auth needed
- **Folder structure preservation** — currently uploads flatten subfolders; preserve `src/foo.js` paths

## Out of scope

Things explicitly **not** planned (in case anyone asks):
- Native desktop app (this is a web app)
- Editing the user's local filesystem (browsers can't; would need an Electron/Tauri wrapper)
- Multi-tenant workspaces (each Supabase user is independent — no team accounts)
- Real-time collaboration / multiple users in one chat

## Notes for future contributors

- Tools live in [backend/Tools/](backend/Tools/) — one file per tool, registered in [\_\_init\_\_.py](backend/Tools/__init__.py)
- File operations all go through [backend/storage.py](backend/storage.py) — never touch disk directly
- LangGraph thread_id is `{user_id}:{session_id}` — never change this format without a checkpoint migration
- Frontend file paths follow Next.js App Router; the workspace UI lives in [ui/app/app/page.tsx](ui/app/app/page.tsx)
- See [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md) for branching/PR rules
