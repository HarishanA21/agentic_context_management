"""MCP runtime: convert a user's enabled MCP server rows into LangChain
tools that the agent can invoke.

Slice B (this version): stdio + streamable_http.
Slice C extends: sse + http.

Implementation note — connection-based tools:
  We use langchain-mcp-adapters' `Connection`-based variant of
  `load_mcp_tools`. Passing only a connection (no live `ClientSession`)
  makes each tool open + close its own short-lived MCP session on every
  invocation. That trades a little latency for greatly simpler lifecycle
  — no pool to drain, no leaks if a request is cancelled, and we can
  rebuild the agent's tool list per request without managing session
  ownership across async boundaries.

Secrets are encrypted at rest with Fernet (see `MCP_SECRET_KEY` env).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.tools import BaseTool


# ── Secret encryption ──────────────────────────────────────────────────


def _fernet():
    from cryptography.fernet import Fernet

    key = os.environ.get("MCP_SECRET_KEY", "").strip()
    if not key:
        key = Fernet.generate_key().decode()
        os.environ["MCP_SECRET_KEY"] = key
        print(
            f"[mcp] NOTE: MCP_SECRET_KEY was unset; generated ephemeral key "
            f"{key[:8]}… Paste this into backend/.env to keep secrets across "
            f"restarts.",
            flush=True,
        )
    return Fernet(key.encode())


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    return _fernet().decrypt(ciphertext.encode()).decode()


def encrypt_env_map(env: Dict[str, str]) -> str:
    if not env:
        return ""
    return encrypt_secret(json.dumps(env))


def decrypt_env_map(ciphertext: str) -> Dict[str, str]:
    if not ciphertext:
        return {}
    raw = decrypt_secret(ciphertext)
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(k): str(v) for k, v in decoded.items()}


# ── Connection builders ─────────────────────────────────────────────────


def _recheck_url(url: str, row: Dict[str, Any]) -> None:
    """Re-resolve the hostname right before connecting (DNS-rebind defence).

    Imports lazily so mcp_client is usable in a test rig that doesn't
    bother with the security module.
    """
    try:
        from mcp_security import recheck_url_before_connect

        recheck_url_before_connect(
            url, allow_catalog_domain=bool(row.get("catalog_slug"))
        )
    except ImportError:
        return  # security module unavailable — skip silently in tests


def _build_auth_headers(row: Dict[str, Any]) -> Dict[str, str]:
    auth_kind = (row.get("auth_kind") or "none").lower()
    if auth_kind == "none" or not row.get("secret_blob"):
        return {}
    try:
        secret = decrypt_secret(row["secret_blob"])
    except Exception:
        return {}
    if auth_kind == "bearer":
        return {"Authorization": f"Bearer {secret}"}
    if auth_kind == "api_key_header":
        header = row.get("auth_header") or "Authorization"
        return {header: secret}
    return {}


def _connection_for_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build a langchain-mcp-adapters Connection dict from a DB row.

    Returns None if the row is malformed (we log and skip rather than
    raise, so one broken row doesn't take down the agent build).
    """
    transport = (row.get("transport") or "").lower()
    if transport == "stdio":
        command = row.get("command") or ""
        args = row.get("args_json") or []
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = []
        if not isinstance(args, list):
            args = []
        env_map: Dict[str, str] = {}
        if row.get("auth_kind") == "api_key_env" and row.get("secret_blob"):
            env_map = decrypt_env_map(row["secret_blob"])
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        env.update(env_map)
        if not command:
            return None
        return {
            "transport": "stdio",
            "command": command,
            "args": [str(a) for a in args],
            "env": env,
        }
    if transport == "streamable_http" or transport == "http":
        url = row.get("endpoint_url")
        if not url:
            return None
        _recheck_url(url, row)
        return {
            "transport": "streamable_http",
            "url": url,
            "headers": _build_auth_headers(row) or None,
        }
    if transport == "sse":
        url = row.get("endpoint_url")
        if not url:
            return None
        _recheck_url(url, row)
        return {
            "transport": "sse",
            "url": url,
            "headers": _build_auth_headers(row) or None,
        }
    return None


# ── Tool loading ────────────────────────────────────────────────────────


async def list_tools_for_row(row: Dict[str, Any]) -> List[BaseTool]:
    """Return LangChain tools for one MCP server row.

    Tools are bound to a connection spec; each tool invocation opens its
    own session under the hood. The session that lists the tools is closed
    before this function returns.
    """
    from langchain_mcp_adapters.tools import load_mcp_tools

    conn = _connection_for_row(row)
    if conn is None:
        return []
    tools = await load_mcp_tools(
        None,
        connection=conn,
        server_name=row.get("name") or row.get("catalog_slug") or "mcp",
    )
    # Stamp origin so the UI can attribute tool calls to their MCP.
    origin = row.get("name") or row.get("catalog_slug") or "mcp"
    for t in tools:
        try:
            setattr(t, "_mcp_origin", origin)
        except Exception:
            pass
    return tools


async def discover_tools_for_row(
    row: Dict[str, Any],
) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """Return a JSON-safe summary for caching in tools_json.

    Returns `(tools, error)` — on failure, `tools=[]` and `error` is a short
    string the UI can show.
    """
    try:
        tools = await list_tools_for_row(row)
        return (
            [
                {"name": t.name, "description": (t.description or "")[:240]}
                for t in tools
            ],
            None,
        )
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:240]}"


async def collect_tools_for_user(
    enabled_rows: List[Dict[str, Any]],
) -> List[BaseTool]:
    """Acquire tools across every enabled row. One broken server is logged
    and skipped, not propagated — the agent still runs with whatever
    actually works."""
    out: List[BaseTool] = []
    for row in enabled_rows:
        try:
            tools = await list_tools_for_row(row)
            out.extend(tools)
        except Exception as e:
            print(
                f"[mcp] tool discovery failed for {row.get('name')!r}: "
                f"{type(e).__name__}: {e}",
                flush=True,
            )
    return out
