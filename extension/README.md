# ACM Extension — context management for any IDE

This folder is **self-contained**. It turns the context-management engine that
already powers the website (in [`../backend`](../backend)) into something usable
from VSCode, Cursor, Antigravity, Claude Code, and Windsurf — **without changing
a single line of the website's code.**

It reaches the IDEs through three surfaces (see
[`../EXTENSION_PLAN.md`](../EXTENSION_PLAN.md) for the why):

| Folder | Surface | What it is |
|---|---|---|
| `acm_gateway/` | **Gateway** | A local OpenAI/Anthropic-compatible proxy. The IDE talks to it as if it were the AI; it applies every technique on the wire, then forwards to the real provider. Full feature parity. |
| `acm_mcp/` | **MCP** | An MCP server exposing `remember` / `recall` / `compact` / `set_profile` tools. Works in *every* MCP-capable IDE. |
| `adapters/` | **Hooks + config** | Per-IDE glue: Claude Code hooks, Cursor `mcp.json`, VSCode notes. |
| `acm_engine/` | — | Thin bridge that imports the techniques from `../backend` so there is **one** source of truth. |

## How it reuses the website's engine

`acm_engine/` puts `../backend` on `sys.path` and re-exports the pure technique
functions (`trim_tool_results`, `summarise_old_messages`,
`sliding_window_trim`, `evict_stale_images`, `annotate_cache_breakpoints`) plus
the `Profile` schema. Nothing in `../backend` is edited. The website and the
extension share the same code; fix a bug once, both benefit.

## Quick start (gateway)

```bash
cd extension
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e .

# point it at any OpenAI-compatible upstream (OpenRouter shown)
export ACM_UPSTREAM_BASE_URL=https://openrouter.ai/api/v1
export ACM_UPSTREAM_API_KEY=sk-or-v1-...

acm-gateway            # serves http://127.0.0.1:8807
```

Then tell the IDE the AI lives at `http://127.0.0.1:8807/v1` (Cursor: custom
OpenAI base URL; Claude Code: `ANTHROPIC_BASE_URL`).

## Config

A single [`config/acm.config.example.json`](config/acm.config.example.json)
holds the active profile (which techniques are on). Copy it to
`config/acm.config.json` and edit, or set `ACM_CONFIG` to its path. The schema
is the website's `Profile` model — same presets (`minimal`, `long_chat`,
`power_research`, `cheap_long`, `visual_recall`).

## Status

- **Gateway** — runnable. OpenAI (`/v1/chat/completions`) **and** Anthropic
  (`/v1/messages`) surfaces; full technique pipeline (trim / summarise / sliding
  window / image-evict / cache breakpoints / **visual method** — rasterise big
  tool outputs to images); **manual message removal** (cascade-safe drop-list,
  persisted to `~/.acm/dropped.json`); **multi-provider routing** (OpenAI /
  OpenRouter / Google / Azure on the OpenAI surface, Anthropic native; creds in
  `~/.acm/providers.json`; route by `x-acm-provider` header, `slug/model`
  prefix, or default — Bedrock via OpenRouter since it needs AWS SigV4); control
  plane (`/status`, `/profile`, `/memory`, `/compact`, `/conversations`,
  `/messages*`, `/providers*`).
- **MCP server** — runnable: memory (`remember`/`recall`), `compact`,
  `set_profile`/`status`, manual removal (`list_messages`/`drop_message`/
  `restore_message`), providers (`list_providers`/`add_provider`/
  `set_default_provider`), **sub-agent** (`spawn_subagent`), and **JIT retrieval**
  (`find_files`/`read_slice`/`grep_files`).
- **Claude Code** — gateway + hooks (trim + memory) + MCP. Done.
- **Cursor** — gateway + hooks (capture + **compact-on-stop**) + MCP. Done.
- **VSCode** — full extension (Language Model Tools + `@acm` chat participant +
  webview settings) talking to the gateway; MCP drop-in for Antigravity /
  Windsurf. TypeScript is written but not yet `tsc`-compiled here (needs
  `npm install` with `@types/vscode`).

Remaining work is marked with `# TODO(acm)` / `TODO(acm)`: orphan-`tool_use`
pruning on the Anthropic path, React components in the VSCode webview, and
auto-starting the gateway from the extension.
