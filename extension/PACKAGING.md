# Packaging & publishing

The extension ships as **one PyPI distribution** — `acm-context-management` —
exposing two console scripts: `acm-gateway` and `acm-mcp`. It is self-contained:
the context-management engine is **vendored** into `acm_engine/_vendor/`, so an
end user never needs the website's `../backend`.

## Before you build: sync the vendored engine

The vendored copy is committed, but refresh it whenever the engine changes in
`backend/`:

```bash
cd extension
python scripts/sync_engine.py     # copies the 3 modules into acm_engine/_vendor/
git add acm_engine/_vendor && git commit -m "sync vendored engine"
```

## Build

```bash
cd extension
uv build                          # -> dist/acm_context_management-0.1.0-py3-none-any.whl + .tar.gz
```

Confirm the wheel is self-contained (contains the vendored engine):

```bash
python -m zipfile -l dist/acm_context_management-0.1.0-py3-none-any.whl | grep _vendor
# acm_engine/_vendor/context_profiles.py, context_editing.py, cache_layout.py
```

## Publish to PyPI

```bash
uv publish                        # uses UV_PUBLISH_TOKEN or ~/.pypirc
# first, test against TestPyPI:
uv publish --publish-url https://test.pypi.org/legacy/
```

## How end users run it

```bash
# zero-install, ephemeral (uvx resolves the package, runs the script):
uvx --from acm-context-management acm-gateway
uvx --from acm-context-management acm-mcp

# or install once:
uv tool install acm-context-management
acm-gateway        # http://127.0.0.1:8807
acm-mcp            # stdio MCP server
```

Required env for the gateway upstream:

```bash
export ACM_UPSTREAM_BASE_URL=https://openrouter.ai/api/v1   # OpenAI-compatible
export ACM_UPSTREAM_API_KEY=sk-or-v1-...
# for the Anthropic /v1/messages surface (Claude Code):
export ACM_ANTHROPIC_API_KEY=sk-ant-...
```

## Docker (optional, for a hosted gateway)

```dockerfile
# extension/Dockerfile  (TODO(acm): add this file)
FROM python:3.12-slim
RUN pip install acm-context-management
EXPOSE 8807
ENV ACM_HOST=0.0.0.0
CMD ["acm-gateway"]
```

Publish to GHCR: `docker build -t ghcr.io/<you>/acm-gateway . && docker push …`.

## Versioning

Bump `version` in `pyproject.toml` (single source). Tag releases
`acm-ext-vX.Y.Z`. The VSCode extension has its own version in
`adapters/vscode/package.json` and its own publish flow (see that folder's
README — `vsce publish` + `ovsx publish`).

## What is NOT on PyPI

The IDE adapters (`adapters/claude-code`, `adapters/cursor`,
`adapters/vscode`) are config/glue distributed via the repo and the VS
Marketplace / Open VSX — not pip. Only the gateway + MCP server are Python
distributions.
