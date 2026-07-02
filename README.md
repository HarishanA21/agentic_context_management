# Agentic Context Management (ACM)

**Context management for any IDE.** ACM sits between your coding assistant and the
model provider as a local, drop-in proxy. It applies a pipeline of
context-editing techniques on the wire — trimming stale tool output, evicting old
images, summarising history, sliding-window fallback, cache breakpoints, and
manual message removal — so long agent sessions stay inside the context window
without you managing it by hand.

It works with **VSCode, Cursor, Claude Code, Antigravity, and Windsurf** through
three surfaces (an OpenAI/Anthropic-compatible gateway, an MCP server, and a
VSCode extension) — without changing a single line of the assistant's own code.

> This started as a full-stack LangGraph chat app (the `backend/` + `ui/` +
> `sandbox/` folders). The context-management engine built for that app was
> extracted into a self-contained product under [`extension/`](extension/), which
> is now the focus of the project. The engine has **one source of truth** — the
> extension imports it from `backend/` rather than forking it.

## Repository layout

| Path | What it is |
|---|---|
| [`extension/`](extension/) | **The ACM product.** Gateway + MCP server + VSCode extension. Start here. |
| [`extension/acm_gateway/`](extension/acm_gateway/) | Local OpenAI/Anthropic-compatible proxy that runs the technique pipeline on every turn. |
| [`extension/acm_mcp/`](extension/acm_mcp/) | MCP server exposing `remember` / `recall` / `compact` / retrieval tools to any MCP-capable IDE. |
| [`extension/adapters/vscode/`](extension/adapters/vscode/) | VSCode extension: per-chat Context Window view, onboarding, live technique notices. |
| [`extension/acm_engine/`](extension/acm_engine/) | Thin bridge re-exporting the technique functions from `backend/` — the shared engine. |
| `backend/`, `ui/`, `sandbox/`, `db/` | The original LangGraph web app the engine came from (see below). |
| [`documents/`](documents/) | Design specs and plans (context strategies, model training, feature matrix). |

## Quick start (ACM gateway)

Prerequisites: Python 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
cd extension
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e .

# Point it at any OpenAI-compatible upstream (OpenRouter shown)
export ACM_UPSTREAM_BASE_URL=https://openrouter.ai/api/v1
export ACM_UPSTREAM_API_KEY=sk-or-v1-...

acm-gateway          # serves http://127.0.0.1:8807
```

Then tell your IDE the model lives at `http://127.0.0.1:8807/v1` (OpenAI base URL)
or set `ANTHROPIC_BASE_URL=http://127.0.0.1:8807` for Claude Code. Every request
now flows through the technique pipeline before reaching your provider.

The VSCode extension can install and supervise the gateway for you — see
[`extension/README.md`](extension/README.md) for the full setup, MCP wiring, and
per-IDE adapter status.

## What ACM does on each turn

The gateway applies enabled techniques in a fixed, defensive order (a failure in
one is skipped and surfaced as a notice, never breaking the request):

1. **Visual method** — rasterise oversized tool outputs to images.
2. **Tool-result trimming** — collapse stale large tool results.
3. **Image eviction** — drop old images past a keep-recent window.
4. **Summarization** — compress old history into a summary (needs a summariser key).
5. **Sliding window** — dumb keep-recent safety net.
6. **Cache breakpoints** — annotate the settled prefix for provider caching.

Plus **manual message removal** (a persistent drop-list), **memory**
(`remember`/`recall`), **JIT retrieval**, and a **sub-agent** tool via MCP.
Configuration is a single profile in
[`extension/config/acm.config.example.json`](extension/config/acm.config.example.json)
(copy to `acm.config.json`); presets include `minimal`, `long_chat`,
`power_research`, `cheap_long`, and `visual_recall`.

## The original web app

The `backend/` (FastAPI + LangGraph + LangChain), `ui/` (Next.js), `sandbox/`
(Docker/E2B workspaces), and `db/` folders are a full-stack chat app with
per-user auth, multi-project history, and sandboxed code workspaces. It is where
the context-management engine was first built. It still runs on its own — see
[`documents/`](documents/) and the per-folder configs — but it is no longer the
project's focus. New context-management work happens in [`extension/`](extension/).

## Further reading

- [`extension/README.md`](extension/README.md) — gateway, MCP, and adapter setup
- [`EXTENSION_PLAN.md`](EXTENSION_PLAN.md) — why the three surfaces exist
- [`documents/MODEL_TRAINING_PLAN.md`](documents/MODEL_TRAINING_PLAN.md) — relevance encoder + DPO judge plan
- [`documents/FEATURE_MATRIX.md`](documents/FEATURE_MATRIX.md) — technique coverage per IDE
</content>
</invoke>
