# Claude Code adapter

The richest integration — Claude Code gives all three surfaces at once.

| Surface | File |
|---|---|
| **Gateway** (full parity) | `env.ANTHROPIC_BASE_URL` → the local gateway |
| **Hooks** (capture + shrink tool output, auto-load memory) | `hooks/post_tool_use.py`, `hooks/user_prompt_submit.py` |
| **MCP** (remember/recall/compact/set_profile buttons) | `mcpServers.acm` → `acm-mcp` |

## Install

First, the Python package (provides the `acm-gateway` / `acm-mcp` commands the
hooks and MCP server need):

```bash
uv tool install acm-context-management      # or: cd extension && uv pip install -e .
```

Then pick **one** of:

### A. As a plugin (recommended)

This folder is a complete Claude Code plugin (`.claude-plugin/plugin.json`,
auto-discovered `hooks/hooks.json` + `.mcp.json` + `commands/`). Add the
marketplace (defined at the repo root) and install:

```text
/plugin marketplace add HarishanA21/agentic_context_management
/plugin install acm-context-management@acm-marketplace
```

The plugin wires the hooks (via `${CLAUDE_PLUGIN_ROOT}`) and the `acm` MCP
server automatically, and adds a `/acm-status` command. Restart Claude Code;
check `/hooks` and `/mcp`.

### B. Manual settings (no marketplace)

Merge [`settings.template.json`](settings.template.json) into
`~/.claude/settings.json`, replacing `<EXT>` with the absolute path to the
`extension/` folder. Restart Claude Code and run `/hooks` + `/mcp` to confirm.

### Full-parity gateway (either path)

Start `acm-gateway`, then set `ANTHROPIC_BASE_URL=http://127.0.0.1:8807` (and
`ACM_ANTHROPIC_API_KEY`) so every turn flows through the technique pipeline.

## Notes

- The gateway now serves a **native Anthropic** `/v1/messages` surface, so
  `ANTHROPIC_BASE_URL=http://127.0.0.1:8807` gives full-parity context editing.
  Set the real upstream key the gateway forwards to:
  ```bash
  export ACM_ANTHROPIC_API_KEY=sk-ant-...        # or ANTHROPIC_API_KEY
  # optional: export ACM_ANTHROPIC_BASE_URL=https://api.anthropic.com/v1
  ```
- Hooks alone (no gateway) already give you live tool-output trimming + memory —
  a good zero-gateway starting point.
