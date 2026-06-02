# In-IDE UI plan — React + esbuild sidebar (Claude Code style)

Goal: edit **everything** — techniques, profiles, providers, memory, dropped
messages, status — from a UI instead of config files or curl. Built as a real
React app bundled with esbuild, rendered in **both** placements that share the
**same bundle**:

  * **Sidebar view** — Activity Bar icon → side panel (like the Claude Code
    extension). A `WebviewViewProvider`.
  * **Editor panel tab** — opens as a full-width editor tab via the
    *ACM: Open Settings* command. A `WebviewPanel`.

Both load the same `media/main.js`; a small `mount` flag tells the app which
layout to use (narrow sidebar vs wide panel). Works in VSCode, Cursor, Windsurf,
and Antigravity (all VSCode-based) from one `.vsix`.

A **vanilla** version of the editor-panel tab already ships today
([src/settingsPanel.ts](src/settingsPanel.ts), command *ACM: Open
Context-Management Settings*) — the work below upgrades it to React and adds the
sidebar placement.

---

## How a webview UI actually works

The webview is a sandboxed mini-browser. It can't call the network cleanly, so:

```
React app (webview)  ──postMessage──▶  extension host (Node)  ──HTTP──▶  acm-gateway
        ▲                                      │
        └──────────── postMessage ◀────────────┘   (results / pushed events)
```

The host already has [src/acmClient.ts](src/acmClient.ts) (HTTP to the gateway).
We add a tiny typed **RPC bridge**: the React app calls `rpc('getProfile')`, the
host runs `acmClient.getProfile()` and posts the result back. No secrets or
network logic live in the webview.

> Note: Microsoft **deprecated** `@vscode/webview-ui-toolkit`. We'll use plain
> React + VSCode theme CSS variables (`var(--vscode-*)`) so it matches the user's
> theme without a dead dependency.

---

## Proposed file structure (new files in `extension/adapters/vscode/`)

```
package.json            # + viewsContainers/views, react/esbuild deps, build scripts
tsconfig.json           # host build (commonjs, unchanged)
tsconfig.ui.json        # webview build (esnext, jsx: react-jsx)
esbuild.mjs             # bundles src/ui/index.tsx -> media/main.js (+ css)
media/                  # esbuild OUTPUT (main.js, main.css) — shipped in the vsix
src/
  extension.ts          # activate: register SidebarProvider + commands (edit)
  sidebarView.ts        # WebviewViewProvider: HTML shell + CSP/nonce + RPC dispatch
  rpc.ts                # host-side method table -> acmClient
  acmClient.ts          # (exists) HTTP client
  ui/
    index.tsx           # mount React, acquireVsCodeApi, wire the bridge
    bridge.ts           # webview-side rpc(): postMessage + promise map
    App.tsx             # tab shell + gateway-unreachable banner
    tabs/Techniques.tsx # per-technique toggles + params  (the core editor)
    tabs/Profiles.tsx   # apply preset / save custom
    tabs/Providers.tsx  # add / edit / delete / default / fetch models
    tabs/Memory.tsx     # list / add / clear
    tabs/Messages.tsx   # drop / restore (manual removal)
    tabs/Status.tsx     # upstream, active techniques, recent events (auto-refresh)
    styles.css          # theme via var(--vscode-*)
```

---

## Phase A — gateway API gaps (so the UI can edit *everything*)

Most endpoints already exist. Two small additions:

| Need | Change | File |
|---|---|---|
| Edit `visual_method` from the UI | `GET /profile` also returns `visual_method`; `POST /profile` accepts an optional `visual_method` block and writes it into the same config file | `acm_gateway/app.py`, `config.py` |
| Clear memory from the UI | `POST /memory/clear {scope}` (and `recall` with empty query already lists) | `acm_gateway/app.py` |
| (optional) Test a provider | `POST /providers/{slug}/test` → one cheap call, returns ok/err | `acm_gateway/app.py` |

Everything else the UI needs already exists: `/status`, `/profile` (body),
`/providers*`, `/memory/*`, `/conversations`, `/messages*`, `/v1/models`.

---

## Phase B — scaffold + prove the pipeline (Status tab only)

1. **Deps + build:** add `react`, `react-dom`, `@types/react*`, `esbuild`;
   `esbuild.mjs` bundles `src/ui/index.tsx` → `media/main.js`; npm scripts:
   `build:host` (tsc), `build:ui` (esbuild), `compile` runs both, `watch`.
2. **Manifest:** `package.json` contributes BOTH placements —
   `viewsContainers.activitybar` (an "ACM" container + icon) + a webview `views`
   entry `acm.sidebar`, **and** keeps the `acm.openSettings` command for the
   editor-panel tab.
3. **Host:** a shared `renderHtml(webview, mount)` helper builds the CSP+nonce
   shell that loads `media/main.js`. `sidebarView.ts` (a `WebviewViewProvider`,
   `mount="sidebar"`) and the `acm.openSettings` command (a `WebviewPanel`,
   `mount="panel"`) both use it and dispatch the same RPC → `acmClient`.
4. **Webview:** minimal React app with just the **Status** tab (calls
   `rpc('status')`, auto-refreshes every few seconds).

Deliverable: the ACM icon appears in the Activity Bar; clicking it shows live
gateway status. This proves manifest + bundling + RPC end-to-end before building
the heavy tabs.

---

## Phase C — Techniques tab (the core editor)

- Render each technique from the active profile with a checkbox + its key params:
  - `tool_result_trimming` (trigger_tokens, keep_recent, exclude_tools)
  - `summarization` (trigger_tokens, keep_recent, summariser_model)
  - `sliding_window` (keep_recent)
  - `image_recall` (mode: off/cache/evict/cache_evict, keep_recent_images, ttl)
  - `memory`, `subagent`, `jit_tools` (enable flags — note: loop-level, only
    active where an agent loop exists)
  - `visual_method` (enable, trigger_tokens)
- **Save** builds a full `Profile` body + `visual_method` and `POST /profile`.
- A "dirty" indicator + Save/Revert; success toast via `vscode.window`.

---

## Phase D — Providers tab

- Table of configured providers (masked key, type, ★ default).
- **Add/Edit** form keyed by type (openai/openrouter/google/azure/anthropic):
  shows only the fields that type needs (azure → endpoint + api_version).
- Buttons: Save (`POST /providers`), Delete, Make default, **Fetch models**
  (`/v1/models`) to populate a model dropdown.

---

## Phase E — Memory + Messages tabs

- **Memory:** list (`recall ''`), add note (scope user/thread), clear scope.
- **Messages:** pick a conversation (`/conversations`), list its messages with
  role + preview, 🗑 remove / ↩ restore (the manual-removal drop-list). Note that
  the IDE's own transcript still shows the bubble (model won't see it).

---

## Phase F — polish + package

- Gateway-unreachable banner with a "Start gateway" hint + a Settings field for
  `acm.gatewayUrl`.
- Auto-refresh Status/Events; debounce saves.
- Theming via `var(--vscode-*)`; keyboard-accessible controls.
- `.vscodeignore` ships `media/` (bundle) but not `src/ui` sources.
- Package: `npm run compile && npx @vscode/vsce package` → install the `.vsix`
  privately in any IDE (`code/cursor --install-extension …`).

---

## Build / toolchain changes summary

| Item | Before | After |
|---|---|---|
| Bundler | none | `esbuild` for the webview app |
| UI deps | none | `react`, `react-dom`, `@types/react`, `@types/react-dom` |
| tsconfig | one (host) | host + `tsconfig.ui.json` (jsx) |
| npm `compile` | `tsc` | `tsc` (host) **and** `esbuild` (ui) |
| Shipped | `out/` | `out/` + `media/` |

---

## Risks / notes

- **No compile in this dev sandbox** — first real `npm run compile` happens on
  your machine; expect to fix minor type/JSX nits there.
- **CSP**: strict `script-src 'nonce-…'`; the webview makes **no** network calls
  (the host does), so we don't need to relax `connect-src`.
- **One webview, all IDEs**: Cursor/Windsurf/Antigravity render the same view; no
  per-IDE UI code.
- **State source of truth stays the gateway** — the UI is a thin editor over the
  gateway's config + stores, so the CLI/MCP and the UI never disagree.

---

## Sequencing

A (gateway gaps) → B (scaffold + Status, proves the pipeline) → C (Techniques) →
D (Providers) → E (Memory + Messages) → F (polish + .vsix). Each phase is
independently testable; B is the unblock for everything else.
