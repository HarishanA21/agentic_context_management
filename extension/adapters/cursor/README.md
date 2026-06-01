# Cursor adapter

Cursor gives all three surfaces: **MCP** (everywhere), **hooks**
(`beforeSubmitPrompt` / `beforeShellExecution` / `afterFileEdit` / `stop`), and
the **gateway** (via a custom OpenAI base URL).

## Install

1. `cd extension && uv pip install -e .` so `acm-mcp` / `acm-gateway` are on PATH.
2. **MCP buttons:** copy [`mcp.json`](mcp.json) to `.cursor/mcp.json` in your
   project (or `~/.cursor/mcp.json`). Restart Cursor; confirm the `acm` server
   under Settings → MCP.
3. **Hooks (capture conversation + tool/file activity):** copy
   [`hooks.json`](hooks.json) to `.cursor/hooks.json` (or `~/.cursor/hooks.json`)
   and replace `<EXT>` with the absolute path to `extension/`. Restart Cursor.
   The captured trail lands in the local memory store (`~/.acm/memory.json`),
   readable via the `recall` MCP tool.
4. **Full-parity context editing (optional):** start `acm-gateway`, then in
   Cursor → Settings → Models, override the OpenAI Base URL to
   `http://127.0.0.1:8807/v1` and set your key. Every request now flows through
   the technique pipeline.

## The hooks

| Hook | Script | Captures |
|---|---|---|
| `beforeSubmitPrompt` | `hooks/before_submit_prompt.py` | the user's prompt ("every conversation") |
| `beforeShellExecution` | `hooks/before_shell_execution.py` | each shell command the agent runs |
| `afterFileEdit` | `hooks/after_file_edit.py` | files the agent touched |
| `stop` | `hooks/stop.py` | end-of-session marker |

All scripts share `hooks/_common.py`, are defensive (never block a turn), and
write to the same `acm_mcp.memory_store` the Claude Code hooks use. Field names
follow Cursor's Agent Hooks schema — adjust the getters in `_common.py` if Cursor
changes them.

On **`stop`**, the hook sends the captured trail to the running gateway's
`POST /compact` endpoint for a real LLM summary and **replaces the raw trail
with that carry-over note** (so the next session's `recall` is tidy). If the
gateway is down or has no API key, the raw trail is kept untouched. Override the
gateway URL with `ACM_GATEWAY_URL` (default `http://127.0.0.1:8807`).
