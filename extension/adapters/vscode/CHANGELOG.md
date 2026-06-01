# Changelog

All notable changes to the ACM VSCode extension.

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
