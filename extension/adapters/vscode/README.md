# ACM — VSCode extension (and Antigravity / Windsurf via Open VSX)

A real VSCode extension that brings the ACM context-management techniques into
the editor. It talks to the local **acm-gateway** over HTTP (the extension runs
in Node and can't import the Python engine directly), and wires three things
into VSCode:

1. **Language Model Tools** — `acm_remember`, `acm_recall`, `acm_compact`,
   `acm_set_profile`. Callable by Copilot **agent mode** and referenceable in
   chat as `#acmRecall` etc.
2. **Commands** — *ACM: Open Context-Management Settings* (webview panel),
   *ACM: Show Gateway Status*, *ACM: Recall Memory*.
3. **`@acm` chat participant** — `@acm status`, `@acm recall <query>`.

For IDEs/models that allow a custom endpoint, also point them at
`http://127.0.0.1:8807/v1` for full-parity context editing (same as Cursor).

## Two ways to use it

**A. MCP only (no build).** Copy [`mcp.json`](mcp.json) to `.vscode/mcp.json`.
VSCode 1.99+ (and Antigravity / Windsurf in their own settings) then expose the
ACM tools to the agent. Zero compilation.

**B. The full extension.** Build + run it:

```bash
cd extension/adapters/vscode
npm install
npm run compile          # tsc -> out/
# press F5 in VSCode to launch an Extension Development Host
```

Requires the gateway running (`acm-gateway`) and `acm.gatewayUrl` pointing at it
(default `http://127.0.0.1:8807`).

## Layout

| File | Role |
|---|---|
| `package.json` | manifest — commands, `languageModelTools`, `chatParticipants`, config |
| `src/extension.ts` | activate: register tools + commands + chat participant |
| `src/acmClient.ts` | HTTP client for the gateway control plane |
| `src/tools.ts` | Language Model Tool implementations |
| `src/settingsPanel.ts` | webview settings panel (lists presets, techniques, recent edits) |
| `mcp.json` | drop-in MCP registration (option A) |

## Gateway endpoints this uses

`GET /status`, `GET /profile`, `POST /profile`, `POST /memory/remember`,
`GET /memory/recall`, `POST /compact` — all added to `acm_gateway` for this
extension.

## Publishing

```bash
npm i -g @vscode/vsce
vsce package                       # -> acm-context-management-0.1.0.vsix
vsce publish                       # VS Marketplace (needs a publisher + PAT)
npx ovsx publish *.vsix            # Open VSX -> Cursor / Antigravity / Windsurf
```

One Open VSX publish covers Cursor, Antigravity, and Windsurf, which all install
from it.

## TODO(acm)

- Bundle the website's React components
  ([`../../../ui/components/context-profiles.tsx`](../../../ui/components/context-profiles.tsx),
  [`../../../ui/components/strategy-demo.tsx`](../../../ui/components/strategy-demo.tsx))
  into the webview instead of the hand-rolled HTML in `settingsPanel.ts`.
- Per-technique toggles in the panel (currently preset-level only) via
  `POST /profile` with a full `body`.
- Auto-start the gateway as a child process from the extension.
