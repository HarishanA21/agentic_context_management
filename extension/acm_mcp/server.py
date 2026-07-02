"""MCP server exposing ACM context-management as tools.

The host AI (in any MCP-capable IDE) can call:
  * ``remember(text, scope)``      — persist a fact locally
  * ``recall(query, scope)``       — fetch facts
  * ``compact(transcript, model)`` — summarise a long transcript on demand
  * ``set_profile(name)``          — switch the gateway's active technique preset
  * ``status()``                   — report what's enabled

These are the "buttons" surface (EXTENSION_PLAN §method 3): cooperative — the
model uses them when it decides to. Pairs with the gateway for the automatic
half.

Run with ``acm-mcp`` (stdio transport, which every IDE speaks).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from acm_engine import DEFAULT_PRESET_NAME, PRESET_SUMMARY
from .memory_store import MemoryStore

mcp = FastMCP("acm")
_memory = MemoryStore()

# The same config file the gateway reads (EXTENSION_PLAN §shared settings).
_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
_CONFIG_PATH = _CONFIG_DIR / "acm.config.json"

# Where the running gateway lives — manual-removal tools talk to it over HTTP
# (the gateway is the only place that can enforce removal on every turn).
_GATEWAY = os.getenv("ACM_GATEWAY_URL", "http://127.0.0.1:8807").rstrip("/")


def _gw(method: str, path: str, body: Optional[dict] = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{_GATEWAY}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode() or "{}")


@mcp.tool()
def remember(text: str, scope: str = "user") -> str:
    """Save a fact to local ACM memory. scope = 'user' or 'thread'."""
    n = _memory.remember(text, scope=scope)
    return f"Saved to {scope} memory (now {n} items)."


@mcp.tool()
def recall(query: str = "", scope: str = "user", limit: int = 10) -> str:
    """Recall facts from local ACM memory, optionally filtered by query."""
    hits = _memory.recall(query, scope=scope, limit=limit)
    if not hits:
        return "(no matching memories)"
    return "\n".join(f"- {h}" for h in hits)


@mcp.tool()
def compact(transcript: str, instructions: str = "") -> str:
    """Summarise a long transcript into a short note the agent can keep instead.

    Routes to the gateway's real summariser (``POST /compact``) so this returns a
    genuine LLM summary — the same code the Cursor stop-hook uses, so there is one
    source of truth. If the gateway is unreachable or has no API key configured,
    it falls back to a truncation-based note that is *explicitly labelled* as
    non-LLM, so the agent never mistakes truncated source text for a summary.
    """
    text = transcript.strip()
    if not text:
        return "(nothing to compact — empty transcript)"

    try:
        data = _gw("POST", "/compact", {"text": text, "instructions": instructions})
    except urllib.error.HTTPError as e:
        # e.g. 503 when no upstream API key is configured — read the real reason.
        reason = _http_error_reason(e)
        return _compact_fallback(text, instructions, reason)
    except (urllib.error.URLError, OSError):
        return _compact_fallback(text, instructions, "gateway unreachable")

    summary = data.get("summary") if isinstance(data, dict) else None
    if summary:
        return summary
    reason = (data.get("error") if isinstance(data, dict) else None) or "no summary returned"
    return _compact_fallback(text, instructions, reason)


def _http_error_reason(e: "urllib.error.HTTPError") -> str:
    """Best-effort human reason from a gateway error response body."""
    try:
        payload = json.loads(e.read().decode() or "{}")
        if isinstance(payload, dict) and payload.get("error"):
            return str(payload["error"])
    except Exception:
        pass
    return f"gateway error {e.code}"


def _compact_fallback(text: str, instructions: str, reason: str) -> str:
    """Truncation-based note when the real summariser is unavailable.

    Clearly marked so the caller knows this is NOT an LLM summary — it preserves
    the head of the transcript rather than compressing it."""
    head = text
    if len(head) > 4000:
        head = head[:4000] + " …[truncated]"
    return (
        f"<summary note=\"compacted without LLM — {reason}\">\n"
        "Manual compaction fallback. Preserve: open tasks, decisions, file "
        "paths/identifiers, concrete results. Source excerpt below (not "
        "summarised).\n"
        f"{instructions}\n\n{head}\n"
        "</summary>"
    )


@mcp.tool()
def set_profile(name: str) -> str:
    """Switch the active technique preset by writing the shared config file.

    Accepts a built-in preset name (minimal / long_chat / power_research /
    cheap_long / visual_recall). The gateway re-reads the file per request, so
    the change takes effect on the next turn.
    """
    if name not in PRESET_SUMMARY:
        return (
            f"Unknown preset '{name}'. Choose one of: "
            + ", ".join(sorted(PRESET_SUMMARY))
        )
    # Resolve the preset body from the engine's built-ins.
    from acm_engine import BUILTIN_PRESETS

    body = next((p["body"] for p in BUILTIN_PRESETS if p["name"] == name), None)
    if body is None:
        return f"Preset '{name}' has no body."
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(body, indent=2))
    return f"Active profile set to '{name}'. {PRESET_SUMMARY[name]}"


@mcp.tool()
def status() -> str:
    """Report the active preset + available presets."""
    active = "(default) " + DEFAULT_PRESET_NAME
    if _CONFIG_PATH.is_file():
        active = f"custom config at {_CONFIG_PATH}"
    lines = [f"Active: {active}", "", "Presets:"]
    lines += [f"- {k}: {v}" for k, v in PRESET_SUMMARY.items()]
    return "\n".join(lines)


@mcp.tool()
def list_messages(conversation: str = "") -> str:
    """List the current conversation's messages with a short id and preview, so
    you can pick one to remove. Requires the gateway to be running."""
    try:
        data = _gw("GET", f"/messages?conv={conversation}")
    except (urllib.error.URLError, OSError) as e:
        return f"Can't reach the gateway: {e}. Start acm-gateway."
    rows = data.get("messages", [])
    if not rows:
        return "(no messages seen yet — send a turn through the gateway first)"
    lines = [f"conversation: {data.get('conversation')}"]
    for r in rows:
        mark = "🗑 " if r.get("dropped") else "   "
        lines.append(f"{mark}[{r['fp']}] {r['role']}: {r['preview']}")
    return "\n".join(lines)


@mcp.tool()
def drop_message(fp: str, conversation: str = "") -> str:
    """Permanently remove a message from the model's context window by its
    fingerprint (from list_messages). The gateway strips it — and any dependent
    tool call/result — from every future turn. Use restore_message to undo."""
    try:
        data = _gw("POST", "/messages/drop", {"fp": fp, "conv": conversation or None})
    except (urllib.error.URLError, OSError) as e:
        return f"Can't reach the gateway: {e}. Start acm-gateway."
    if not data.get("ok"):
        return f"Failed: {data.get('error', 'unknown error')}"
    return f"Removed [{fp}]. Dropped in this conversation: {len(data.get('dropped', []))}."


@mcp.tool()
def restore_message(fp: str, conversation: str = "") -> str:
    """Undo a drop_message — let the model see the message again."""
    try:
        data = _gw("POST", "/messages/restore", {"fp": fp, "conv": conversation or None})
    except (urllib.error.URLError, OSError) as e:
        return f"Can't reach the gateway: {e}. Start acm-gateway."
    return f"Restored [{fp}]." if data.get("ok") else f"[{fp}] was not dropped."


@mcp.tool()
def spawn_subagent(task: str, context: str = "") -> str:
    """Delegate a focused sub-task to an isolated sub-agent and get back only
    its conclusion — the sub-agent's reasoning never enters this conversation's
    context. Use for heavy exploration/analysis you want summarised. Requires
    the gateway running with a provider key."""
    try:
        data = _gw("POST", "/subagent", {"task": task, "context": context})
    except (urllib.error.URLError, OSError) as e:
        return f"Can't reach the gateway: {e}. Start acm-gateway."
    return data.get("summary") or f"Failed: {data.get('error', 'unknown error')}"


@mcp.tool()
def find_files(pattern: str) -> str:
    """JIT retrieval: list workspace files matching a glob (e.g. '*.py'), paths
    + sizes only, capped — so you read just what you need next."""
    from .jit import find_files as _ff

    return _ff(pattern)


@mcp.tool()
def read_slice(path: str, mode: str = "head", lines: int = 40) -> str:
    """JIT retrieval: read the first ('head') or last ('tail') N lines of a
    workspace file, capped to ~8 KB — instead of dumping the whole file."""
    from .jit import read_slice as _rs

    return _rs(path, mode=mode, lines=lines)


@mcp.tool()
def grep_files(pattern: str, glob: str = "*") -> str:
    """JIT retrieval: regex-search workspace files (name matching 'glob') and
    return capped 'path:line: text' matches."""
    from .jit import grep_files as _gf

    return _gf(pattern, glob=glob)


@mcp.tool()
def list_providers() -> str:
    """List the LLM providers configured in the gateway and the default."""
    try:
        data = _gw("GET", "/providers")
    except (urllib.error.URLError, OSError) as e:
        return f"Can't reach the gateway: {e}. Start acm-gateway."
    provs = data.get("providers", {})
    lines = [f"default: {data.get('default') or '(env fallback)'}"]
    for slug, c in provs.items():
        lines.append(f"- {slug} [{c.get('type')}] key={c.get('api_key', '—')}")
    lines.append("supported types: " + ", ".join(data.get("supported_types", [])))
    return "\n".join(lines)


@mcp.tool()
def add_provider(
    slug: str,
    type: str,
    api_key: str = "",
    base_url: str = "",
    azure_endpoint: str = "",
    api_version: str = "",
    make_default: bool = False,
) -> str:
    """Configure a provider in the gateway. type ∈ openai|openrouter|google|
    azure|anthropic|custom. For azure, pass azure_endpoint (+ optional
    api_version); the model is the deployment name."""
    payload = {"slug": slug, "type": type, "default": make_default}
    for k, v in (
        ("api_key", api_key),
        ("base_url", base_url),
        ("azure_endpoint", azure_endpoint),
        ("api_version", api_version),
    ):
        if v:
            payload[k] = v
    try:
        data = _gw("POST", "/providers", payload)
    except (urllib.error.URLError, OSError) as e:
        return f"Can't reach the gateway: {e}. Start acm-gateway."
    if not data.get("ok"):
        return f"Failed: {data.get('error', 'unknown error')}"
    return f"Configured '{slug}'. Default: {data.get('default')}."


@mcp.tool()
def set_default_provider(slug: str) -> str:
    """Set which configured provider new requests use by default."""
    try:
        data = _gw("POST", f"/providers/{slug}/default")
    except (urllib.error.URLError, OSError) as e:
        return f"Can't reach the gateway: {e}. Start acm-gateway."
    return f"Default provider: {data.get('default')}" if data.get("ok") else f"Unknown provider '{slug}'."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
