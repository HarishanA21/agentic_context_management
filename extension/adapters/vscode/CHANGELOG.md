# Changelog

All notable changes to the Context Management — ACM VSCode extension.

## [0.2.8] - 2026-07-07

### Fixed
- Rebuilt the webview bundle so the two-column chat detail and Graph tab
  actually ship. 0.2.7 shipped a stale bundle that still rendered a single
  column.

## [0.2.7] - 2026-07-07

### Changed
- Chat detail is two columns again: the conversation on the left, settings,
  techniques, and cleanup in a right side rail.
- Grouped view is the default. Switch to Raw for the flat list.

### Added
- Graph view is back in the chat detail. A third tab next to Grouped and Raw
  shows the per request context timeline.
- Remove and Restore now push a live update event, so every open ACM view
  refreshes right away when a message is dropped or brought back.

## [0.2.6] - 2026-07-07

### Changed
- Chat detail now shows each message once, in a single column. The duplicate
  context window panel is gone.
- Messages start collapsed with a short preview. Use "Show full message" to
  expand and "Show less" to fold back.
- Rewrote the README in plain language for the Marketplace and Open VSX pages.

### Added
- Inline images on a message. Tool screenshots and visual method pages now
  render right in the conversation.

## [0.2.5] - 2026-07-03

### Added
- Savings dashboard — tokens/cost freed per technique, a request preview, undo
  for the last edit, and a training-data export.
- Degraded-mode notices — technique failures now surface as visible UI
  notices instead of failing silently.
- Realtime event channel for context-window updates.

### Changed
- Merged the Profiles tab into Techniques (presets apply inline) and
  collapsed the Overview tab's gateway/activity detail behind a summary line.
- Memory recall now ranks by IDF-weighted token overlap.
- MCP `compact` routes through the gateway summariser.
- Centralised gateway state paths and switched to atomic writes.

## [0.2.4] - 2026-06-30

### Added
- First-run onboarding — an animated flow diagram (This PC → ACM Gateway →
  Model server) that live-checks each hop, confirms everything is active, and
  hands you a copy-paste recipe to try it from Claude Code before a "Get
  started" button drops you into the panel.
- Zero-setup gateway — on first activation the extension auto-installs the
  acm-gateway (bootstrapping `uv` if needed) and supervises it, so the
  extension works on install with no manual `uv tool install` step.
- Context Window view — a live HUD of the current conversation's context-token
  usage, with manual drop-list editing.
- Claude Code routing — manage `ANTHROPIC_BASE_URL` to route Claude Code through
  the gateway, forwarding your OAuth token (no extra API key needed on a
  subscription). Commands: *Monitor Claude Code* / *Stop Monitoring Claude Code*.
- Managed gateway — the extension can start, supervise, and restart the
  `acm-gateway` process automatically, or adopt one already running.
- Selectable profiles: `minimal`, `long_chat`, `power_research`, `cheap_long`,
  `visual_recall`.

### Changed
- Renamed display name to **Context Management — ACM** for the Marketplace.

## [0.1.0] - 2026-06-01

### Added
- Language Model Tools: `acm_remember`, `acm_recall`, `acm_compact`,
  `acm_set_profile` (callable from Copilot agent mode and `#tool` references).
- `@acm` chat participant — `@acm status`, `@acm recall <query>`.
- Commands: *Open Context-Management Settings* (webview), *Show Gateway Status*,
  *Recall Memory*.
- Webview settings panel listing presets, active techniques, and recent
  context edits, talking to the local acm-gateway control plane.
- `mcp.json` drop-in for zero-build MCP registration (also used by Antigravity /
  Windsurf).
