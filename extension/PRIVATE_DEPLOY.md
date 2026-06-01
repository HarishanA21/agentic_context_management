# Private deployment — use it for real, publish nothing public

The public marketplaces (PyPI, VS Marketplace, Open VSX, the Claude plugin
marketplace) are **public by nature** — there's no practical "private listing".
So to use this privately you do **not** publish there. You distribute **local
artifacts** (a `.whl` and a `.vsix`) and, optionally, a **private GitHub repo**.

Everything runs on your machine; your model key is the only thing that leaves it
(to your provider), exactly as before.

| Surface | Private method (no public registry) |
|---|---|
| Gateway + MCP (Python) | install the **wheel** or `uv tool install` from the **private git repo** |
| VSCode extension | build a **`.vsix`** and install it locally (also works in Cursor/Windsurf/Antigravity) |
| Claude Code plugin | add the **private git repo** (or a local path) as a plugin marketplace |
| Cursor / Claude Code hooks + MCP | just local config files — nothing to publish |

---

## 0. Make sure the repo is private

The remote is `github.com/HarishanA21/agentic_context_management`. Confirm it's
**Private** (GitHub → repo → Settings → Danger Zone → Change visibility), or the
plugin-marketplace step below would expose it.

---

## 1. Gateway + MCP (Python) — private install

**This machine (fastest):**
```bash
cd extension
uv tool install --editable .          # acm-gateway + acm-mcp on PATH, tracks your edits
```

**Other machines, from the private repo (no registry):**
```bash
# needs git access to the private repo (gh auth / SSH key)
uv tool install "git+ssh://git@github.com/HarishanA21/agentic_context_management.git#subdirectory=extension"
```

**Or hand off the built wheel** (in `extension/dist/`):
```bash
uv tool install ./acm_context_management-0.1.0-py3-none-any.whl
```

Then run it:
```bash
export ACM_UPSTREAM_BASE_URL=https://openrouter.ai/api/v1
export ACM_UPSTREAM_API_KEY=sk-or-v1-...
acm-gateway
```

> Tip: keep keys out of the repo. Put the `export`s in `~/.acm/env.sh` and
> `source` it, or use a launchd/systemd user service for the gateway.

---

## 2. VSCode extension — private `.vsix` (no marketplace)

```bash
cd extension/adapters/vscode
npm install
npm run compile
npx --yes @vscode/vsce package        # -> acm-context-management-0.1.0.vsix
```

Install the `.vsix` locally — works in every VSCode-family IDE:
```bash
code   --install-extension acm-context-management-0.1.0.vsix   # VSCode
cursor --install-extension acm-context-management-0.1.0.vsix   # Cursor
# Windsurf / Antigravity: Command Palette → "Extensions: Install from VSIX…"
```

No publisher account, no marketplace, nothing public. Re-run `vsce package` +
`--install-extension` to update.

---

## 3. Claude Code plugin — private marketplace

Because the repo is private, add it by **local path** (no network) or via the
**private git** (you're already authenticated with `gh`):

```text
# local path (this machine):
/plugin marketplace add /Users/harishanambihaipahan/agentic_context_management

# or the private repo (any machine with gh auth to it):
/plugin marketplace add HarishanA21/agentic_context_management

/plugin install acm-context-management@acm-marketplace
```

Set `ANTHROPIC_BASE_URL=http://127.0.0.1:8807` so turns flow through the gateway.

---

## 4. Cursor / Antigravity / Windsurf — local config only

Copy the config files (Phase 2/4 of `TESTING.md`); they reference the
locally-installed `acm-mcp` and the local gateway. Nothing is published.

---

## 5. Keep using it (real-time)

1. `acm-gateway` running (a user service is nicest — see tip above).
2. Each IDE pointed at it (endpoint override) and/or the MCP server registered.
3. Edit `extension/config/acm.config.json` (or use the VSCode panel / `set_profile`)
   to flip techniques while you work; watch `curl localhost:8807/status`
   `last_events` to see what fired.

---

## If you later want to share it privately with a teammate

- **Python:** a private package index (GitHub does not host PyPI) — e.g. AWS
  CodeArtifact / Gemfury / a self-hosted `devpi`. Or just send the `.whl`.
- **VSCode:** send the `.vsix`, or host a **private Open VSX**-style gallery
  (advanced). The `.vsix` file is the simplest.
- **Claude Code:** give them read access to the private repo; they
  `/plugin marketplace add` it.

There is no first-party "private marketplace" for any of these, so artifact
files + a private repo is the standard private path.
