# ACM as a Cross-IDE Extension — End-to-End Plan

Goal: ship the context-management techniques built in this repo (trimming,
summarisation, sliding window, memory, sub-agents, JIT tools, image-recall /
visual method, prompt caching, multi-provider routing, context profiles) as a
product usable inside **VSCode, Cursor, Antigravity** (and Claude Code,
Windsurf) — capturing tool output and conversation, and letting users manage
the techniques per-IDE while keeping every feature of the current method.

---

## 0. The architectural truth this plan is built around

In this repo **we own the agent loop**. LangGraph hands us the full message
array and our code (`apply_context_edits` in
[backend/context_editing.py](backend/context_editing.py), `ImageRecallMiddleware`
in [backend/cache_layout.py](backend/cache_layout.py)) rewrites it *before every
model call*. That position is why trimming, summarisation, image eviction, and
cache breakpoints all work.

Inside VSCode/Copilot, Cursor, and Antigravity, **the IDE owns the agent loop
and the context window**. They do not expose an API to rewrite their message
history. So we cannot directly port `apply_context_edits` to "edit their
context." We need a surface where we still see/rewrite model traffic. There are
exactly three, and the product uses all three in layers.

| Surface | What we can see / do | Available in |
|---|---|---|
| **LLM gateway / proxy** (we become the model endpoint) | The **entire** request message array + response. Full rewrite — the LangGraph-equivalent position. | Cursor (custom OpenAI base URL), Claude Code (`ANTHROPIC_BASE_URL`), Antigravity/VSCode where a custom endpoint is allowed |
| **Hooks** (event callbacks) | Tool inputs/outputs, prompt-submit, file edits. Can **replace a tool result before it enters context**. | Claude Code (rich hooks), Cursor (beforeShellExecution / afterFileEdit / beforeSubmitPrompt / stop) |
| **MCP server** (we provide tools/resources) | Only what the host model chooses to call. Cannot auto-edit the window. | Everywhere: Claude Code, Cursor, VSCode, Antigravity, Windsurf |

**Conclusion:** the gateway is the only surface that keeps 100% feature parity
(it occupies the exact position our middleware does today). Hooks give
tool-output capture + compaction. MCP gives the cross-IDE control/memory reach.
The product ships all three.

---

## 1. Target architecture — 3 layers

```
LAYER 1 — acm-core (engine, extracted from backend/)
  context_editing · cache_layout · context_profiles ·
  visual_tool/compressor · memory · subagent · providers
  Pure, framework-free. Ships as a Python pkg AND a thin local service.

        ▲ HTTP/RPC                         ▲ HTTP/RPC
LAYER 2A — acm-gateway                LAYER 2B — acm-mcp (MCP server)
  OpenAI + Anthropic-compatible        tools: compact, remember, recall,
  proxy. Applies ALL techniques        evict_images, set_profile, status
  on the wire (real feature parity).   Works in every MCP-capable IDE.

        ▲
LAYER 3 — IDE adapters (thin)
  • Claude Code: hooks + settings + MCP (richest)
  • Cursor: hooks + MCP + custom model endpoint → gateway
  • VSCode ext: LM Tools API + MCP + webview settings
  • Antigravity / Windsurf: MCP + custom endpoint (Open VSX)
```

Key refactor: make the engine import-clean. `context_editing.py` is already
nearly pure — its only coupling is the lazy `from api import _build_model`
(see [backend/context_editing.py](backend/context_editing.py) ~line 638). Inject
a `model_factory` instead and the engine becomes a reusable library.

---

## 2. Feature → surface parity matrix

| Technique (current method) | Gateway | Hooks | MCP |
|---|---|---|---|
| tool_result_trimming | ✅ rewrite old ToolMessages on the wire | ✅ PostToolUse returns compacted result | ⚠️ only if model calls `compact` |
| summarization | ✅ same as today | ⚠️ on stop/threshold inject summary | ✅ `compact` tool |
| sliding_window | ✅ | ➖ | ➖ |
| memory (`MemoryCfg`) | ✅ | ✅ capture + auto-view-at-start | ✅ `remember`/`recall` (best fit) |
| subagent | ✅ delegate inside gateway | ➖ | ✅ `spawn_subagent` |
| jit_tools | ✅ | ➖ | ✅ MCP *is* JIT tool exposure |
| image_recall (cache + evict) | ✅ full — exactly `ImageRecallMiddleware` | ✅ PostToolUse rasterize + evict | ⚠️ partial |
| visual_tool (rasterize output→PNG) | ✅ | ✅ best fit — rewrite tool stdout to image block | ✅ |
| context_profiles + presets | ✅ per-request header | ✅ read from settings file | ✅ `set_profile` |
| provider abstraction | ✅ gateway routes to any provider | ➖ | ➖ |

The gateway column is every checkmark — that is the headline integration. Hooks
are the value-add where we also want to capture tool output + conversation. MCP
is the lowest-common-denominator reach into VSCode/Antigravity.

---

## 3. Build plan — phased

**Phase 0 — Extract the engine (1–2 wks).** Carve `acm-core` out of `backend/`:
move `context_editing`, `cache_layout`, `context_profiles`, `visual_tool`,
`subagent`, `providers`, memory into a standalone, dependency-light package
(uv-managed). Break the `from api import _build_model` cycle by injecting a
`model_factory`. Add a golden test suite so parity is provable later.
Deliverable: `uv add acm-core` + the existing FastAPI app importing it unchanged.

**Phase 1 — acm-gateway (the real product) (2–3 wks).** A local long-running
service exposing **OpenAI-compatible** `/v1/chat/completions` and
**Anthropic-compatible** `/v1/messages`. Per request: resolve active profile →
run the `apply_context_edits` equivalent on inbound messages → apply
`ImageRecallMiddleware` cache breakpoints → forward to the real provider →
record `context_events` → stream back. This *is* the middleware, at the wire
instead of inside LangGraph. Add a status endpoint the IDE UIs poll.

**Phase 2 — Claude Code adapter (1 wk).** Hooks (`PostToolUse` →
compact/rasterize/store-to-memory; `UserPromptSubmit` → auto-view-memory;
`SessionStart` → load profile) + bundled MCP server + settings template. Point
Claude Code at the gateway via `ANTHROPIC_BASE_URL`. Showcase target — its hook
surface matches our method 1:1. Distribute as a Claude Code plugin.

**Phase 3 — Cursor adapter (1 wk).** Cursor hooks (`beforeShellExecution`,
`afterFileEdit`, `beforeSubmitPrompt`, `stop`) for tool-output capture + memory;
register the MCP server in `.cursor/mcp.json`; optionally route Cursor's custom
model endpoint to the gateway for full parity. Ship as a Cursor extension /
rules + MCP bundle.

**Phase 4 — VSCode extension (2–3 wks).** Most work — no Claude-Code-style hooks.
Use the **Language Model Tools API** + **MCP support** to register techniques as
tools/participants, plus a **webview settings panel** reusing the existing React
[ui/components/context-profiles.tsx](ui/components/context-profiles.tsx) /
[ui/components/strategy-demo.tsx](ui/components/strategy-demo.tsx) components.
Capture conversation via the chat-participant API (`@acm`). Full
auto-context-editing only when the user routes a custom endpoint to the gateway.

**Phase 5 — Antigravity / Windsurf adapter (0.5 wk).** MCP registration + custom
endpoint to the gateway. Thin — their public extensibility is essentially
MCP + model config, and they consume Open VSX from Phase 4.

**Phase 6 — Shared settings & telemetry (1 wk).** One `acm.config.json` schema
(the existing pydantic `Profile` model in
[backend/context_profiles.py](backend/context_profiles.py)) read by every
adapter, plus the `context_events` stream surfaced in each IDE's UI so users
*see* what got trimmed/evicted/cached — the differentiator, already built in
[backend/routes_demo.py](backend/routes_demo.py) + `strategy-demo.tsx`.

---

## 4. Publishing — per channel

- **acm-core / acm-gateway / acm-mcp** → **PyPI** (`uv build` → `uv publish`).
  Gateway also as `uvx acm-gateway` one-liner + a Docker image on GHCR.
- **Claude Code plugin** → a git repo with `.claude-plugin/marketplace.json`;
  users `--add` it. Hooks + MCP + agents bundle inside.
- **Cursor** → submit to Cursor's MCP directory; distribute hooks/rules as an
  installable repo (`.cursor/mcp.json`, `.cursor/rules`).
- **VSCode** → `vsce package` → **VS Marketplace** *and* **Open VSX**. Open VSX
  is what Cursor / Antigravity / Windsurf pull from — one publish covers all
  three. VS Marketplace needs a `publisher` ID via Azure DevOps.
- **Antigravity / Windsurf** → consume Open VSX + MCP, so Phase 4's Open VSX
  publish + the MCP server cover them with near-zero extra work.
- **Discovery** → list the MCP server in the public registries
  (modelcontextprotocol registry, Smithery, mcp.so).

One pipeline, five storefronts: PyPI (engine+gateway+mcp), Claude plugin
marketplace, VS Marketplace, Open VSX (→ Cursor/Antigravity/Windsurf), MCP
registries.

---

## 5. Risks / decisions to watch

1. **Endpoint lock-in.** Gateway parity only where the IDE lets us override the
   model base URL. Cursor + Claude Code do; Copilot's first-party model does not
   (fall back to hooks + MCP there). Confirm per target before promising "all
   features."
2. **Wire-format fidelity.** A drop-in proxy must faithfully reproduce OpenAI
   *and* Anthropic streaming + tool-call schemas, including `cache_control`
   passthrough. Budget real time.
3. **Secrets.** Today provider creds are Fernet-encrypted in Postgres. On a dev
   machine the gateway needs a local secret store (OS keychain) — don't ship
   Postgres as a hard dependency of the desktop product.
4. **Privacy.** Capturing every conversation + tool output via hooks must be
   local-first and opt-in, with a clear data boundary, or it's a non-starter
   for enterprise.
5. **Trademarks.** "Antigravity"/"Cursor"/"VSCode" in names/marketplaces have
   rules — market as "works with," don't imply endorsement.

---

## 6. Suggested sequencing

Phase 0 → Phase 1 (gateway) → Phase 2 (Claude Code, fastest payoff) →
Phase 3 (Cursor) → Phase 4 (VSCode) → Phase 5 (Antigravity/Windsurf) →
Phase 6 (shared settings/telemetry runs alongside from Phase 2 on).

Phase 0 is the unblock for every adapter; do it first.
