# End-to-end testing guide — all IDEs

Test order: **Phase 0 (install + gateway)** → **Phase 0.5 (gateway-only smoke,
no IDE)** → then whichever IDE(s) you want (Phases 1–4). The gateway-only smoke
proves the engine works *before* you add IDE quirks, so do it first.

You need **one model API key** (your own). The cheapest path is an OpenRouter key
with a free model. For the Claude Code native path you also need an Anthropic key.

---

## Phase 0 — one-time setup (shared by every IDE)

```bash
cd extension

# 1. refresh the vendored engine + install the CLIs GLOBALLY so the IDEs can
#    find `acm-gateway` / `acm-mcp` on PATH (not just inside a venv):
python scripts/sync_engine.py
uv tool install --editable .          # gives `acm-gateway` + `acm-mcp` on PATH

# 2. point the gateway at your model provider (OpenRouter shown):
export ACM_UPSTREAM_BASE_URL=https://openrouter.ai/api/v1
export ACM_UPSTREAM_API_KEY=sk-or-v1-...        # your key
# for the Claude Code /v1/messages path, also:
export ACM_ANTHROPIC_API_KEY=sk-ant-...

# 3. start the gateway (leave this terminal running):
acm-gateway                            # http://127.0.0.1:8807
```

**Verify:** in another terminal

```bash
curl -s http://127.0.0.1:8807/status | python -m json.tool
```

✅ You should see `"ok": true`, the upstream URL, and the `techniques` block.
Out of the box `tool_result_trimming` is **on** (the shipped example profile).

> Tip: `uv tool install --editable .` means edits to the Python code need a
> reinstall (`uv tool install --editable . --force`). For rapid iteration you can
> instead run `acm-gateway` from an activated venv (`uv venv && uv pip install -e .`).

---

## Phase 0.5 — gateway-only smoke (no IDE, ~2 min)

This proves every technique fires before any IDE is involved.

### A. Control plane (no model key needed)

```bash
B=http://127.0.0.1:8807
# providers
curl -s -X POST $B/providers -H 'content-type: application/json' \
  -d '{"slug":"or","type":"openrouter","api_key":"sk-or-demo","default":true}'
curl -s $B/providers
# memory
curl -s -X POST $B/memory/remember -H 'content-type: application/json' \
  -d '{"text":"auth lives in login.ts","scope":"user"}'
curl -s "$B/memory/recall?query=auth"
# switch technique preset
curl -s -X POST $B/profile -H 'content-type: application/json' -d '{"name":"visual_recall"}'
curl -s $B/status | python -m json.tool        # image_recall now "cache_evict"
```

✅ Providers list (key masked), memory recall returns the note, status reflects
the preset.

### B. A real turn (needs your model key) — trimming + manual removal

```bash
B=http://127.0.0.1:8807
# send a conversation with a big tool result (use your real model id):
curl -s -X POST $B/v1/chat/completions -H 'content-type: application/json' -d '{
  "model":"meta-llama/llama-3.1-8b-instruct",
  "messages":[
    {"role":"user","content":"what did the tool say?"},
    {"role":"assistant","content":"","tool_calls":[{"id":"c1","type":"function","function":{"name":"grep","arguments":"{}"}}]},
    {"role":"tool","tool_call_id":"c1","name":"grep","content":"BIG OUTPUT ... see https://example.com/x ..."},
    {"role":"user","content":"summarise"}
  ]}' >/dev/null

curl -s $B/messages | python -m json.tool        # see fingerprints (fp) per message
# drop the tool message by its fp:
curl -s -X POST $B/messages/drop -H 'content-type: application/json' -d '{"fp":"<paste-tool-fp>"}'
# re-send the same conversation; the gateway log prints a manual_removal event
curl -s $B/status | python -c "import sys,json;print(json.load(sys.stdin)['last_events'][-3:])"
```

✅ `/messages` lists each message with an `fp`; after a drop, re-sending shows a
`manual_removal` event and the dropped content is gone from what's forwarded.

### C. Visual method (needs a key + multimodal model)

Enable it, then send a big tool result:

```bash
# copy the example config to a user copy and turn on visual_method:
cp config/acm.config.example.json config/acm.config.json
# edit config/acm.config.json -> "visual_method": { "enabled": true }
curl -s $B/status | python -c "import sys,json;print('visual:',json.load(sys.stdin)['techniques']['visual_method'])"
```

Send a chat with a >500-token tool result, then `curl $B/status` → `last_events`
shows a `visual_method` event (`rasterised: 1`). The forwarded tool message is
now `[references] + image`.

---

## Phase 1 — Claude Code

**Install (pick one):**

- **Plugin (recommended):** in Claude Code
  ```
  /plugin marketplace add /Users/harishanambihaipahan/agentic_context_management
  /plugin install acm-context-management@acm-marketplace
  ```
- **Manual:** merge `adapters/claude-code/settings.template.json` into
  `~/.claude/settings.json`, replacing `<EXT>` with the absolute path to
  `extension/`.

**Point Claude Code at the gateway** (for full-parity editing): add to your
Claude Code env / settings:
```
ANTHROPIC_BASE_URL=http://127.0.0.1:8807
```

**Verify + test:**

| Check | How | Expect |
|---|---|---|
| Hooks loaded | run `/hooks` | `PostToolUse` + `UserPromptSubmit` listed |
| MCP loaded | run `/mcp` | server `acm` with its tools |
| Slash command | type `/acm-status` | reports active profile |
| Tool-output trimming | ask it to run a command with huge output | the giant output is shortened in context (check the gateway terminal log) |
| Memory | "remember that X"; new session → it recalls | `remember`/`recall` tools fire |
| Sub-agent | ask it to `spawn_subagent` a research task | only a short summary returns |
| Manual removal | `list_messages` then `drop_message <fp>` | message stops appearing to the model |

---

## Phase 2 — Cursor

**MCP tools:**
```bash
mkdir -p .cursor && cp extension/adapters/cursor/mcp.json .cursor/mcp.json
```
Restart Cursor → Settings → MCP → confirm `acm` is connected.

**Hooks (capture + compact-on-stop):**
```bash
cp extension/adapters/cursor/hooks.json .cursor/hooks.json
# edit .cursor/hooks.json: replace every <EXT> with the absolute path to extension/
```
Restart Cursor.

**Gateway (full-parity editing):** Cursor → Settings → Models → enable "Override
OpenAI Base URL" → `http://127.0.0.1:8807/v1`, set any key (the gateway uses its
own upstream key).

**Verify + test:**

| Check | How | Expect |
|---|---|---|
| MCP | Settings → MCP | `acm` tools listed |
| Capture | run a shell command / edit a file in agent chat | entries appear via `recall` (or in `~/.acm/memory.json`) |
| Compact-on-stop | finish an agent session | the trail collapses to one `[session summary …]` (needs the gateway running) |
| Gateway editing | long chat through the overridden endpoint | `curl localhost:8807/status` → `last_events` show trims |

---

## Phase 3 — VSCode

```bash
cd extension/adapters/vscode
npm install
npm run compile          # tsc -> out/  (also your TypeScript compile check)
```
Open this folder in VSCode and press **F5** → an "Extension Development Host"
window launches with the extension loaded. (Keep the gateway running.)

**Test:**

| Check | How | Expect |
|---|---|---|
| Settings panel | Cmd/Ctrl+Shift+P → **ACM: Open Context-Management Settings** | panel shows techniques, presets, providers, messages |
| Switch preset | click a preset button | `/status` config updates |
| Providers | panel "Providers" → make default | default ★ moves |
| Manual removal | send a turn through the gateway first, then panel → 🗑 remove | row struck through; model stops seeing it |
| Status command | **ACM: Show Gateway Status** | info toast with active techniques |
| LM tools | Copilot **agent mode** → reference `#acmRecall` / `#acmCompact` | tool runs |
| Chat participant | type `@acm status` in chat | reports gateway status |

> No-build option: copy `extension/adapters/vscode/mcp.json` to
> `.vscode/mcp.json` to get the `acm` MCP tools without compiling the extension.

---

## Phase 4 — Antigravity / Windsurf

Both consume MCP + Open VSX. For now:

1. Register the MCP server in the IDE's MCP settings (command `acm-mcp`), or copy
   the `servers` block from `extension/adapters/vscode/mcp.json`.
2. If the IDE allows a custom model endpoint, point it at
   `http://127.0.0.1:8807/v1` for full-parity editing.
3. Verify the `acm` tools appear in the IDE's tool list; test `recall` /
   `set_profile` / `drop_message`.

(The packaged VSCode extension reaches these via Open VSX once published —
`ovsx publish`.)

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `acm-gateway: command not found` | `uv tool install --editable .` from `extension/`, or activate the venv |
| IDE can't reach gateway | is `acm-gateway` running? check `curl localhost:8807/status` and the IDE's endpoint/`acm.gatewayUrl` |
| `no upstream API key` on /compact or /subagent | set `ACM_UPSTREAM_API_KEY` (or configure a provider) and restart the gateway |
| MCP server not appearing | confirm `acm-mcp` is on PATH (`which acm-mcp`); restart the IDE |
| Hooks not firing (Claude Code) | `/hooks`; ensure `python3` resolves and `<EXT>` paths are absolute |
| Manual-removal panel empty | send at least one turn through the gateway first (it records the conversation) |
| Anthropic 4xx via gateway | set `ACM_ANTHROPIC_API_KEY`; Bedrock isn't supported directly — route via OpenRouter |

## Reset test state

```bash
rm -f extension/config/acm.config.json
rm -f ~/.acm/providers.json ~/.acm/memory.json ~/.acm/dropped.json
```
