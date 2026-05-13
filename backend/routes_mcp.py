"""MCP server inventory endpoints.

Slice B scope:
  - GET    /mcp/catalog          — static, code-shipped catalog.
  - GET    /mcp/servers          — caller's mcp_servers rows, secrets redacted.
  - POST   /mcp/servers          — create (catalog-derived or custom).
  - PATCH  /mcp/servers/{id}     — toggle enabled, edit fields, rotate secret.
  - DELETE /mcp/servers/{id}     — remove (custom) / detach + disable (catalog).

Slice C adds:
  - POST /mcp/servers/{id}/test           — connect, discover tools, persist.
  - SSE + HTTP transports via _connection_for_row in mcp_client.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field


router = APIRouter(prefix="/mcp", tags=["mcp"])

# mtime-keyed catalog cache. The catalog file is hand-edited between
# releases; cheking st_mtime on each request means JSON edits show up
# without bouncing uvicorn, but we still don't re-parse on every call.
_CATALOG_CACHE: Optional[Dict[str, Any]] = None
_CATALOG_MTIME: float = 0.0
_CATALOG_PATH = Path(__file__).with_name("mcp_catalog.json")


def _load_catalog() -> Dict[str, Any]:
    global _CATALOG_CACHE, _CATALOG_MTIME
    mtime = _CATALOG_PATH.stat().st_mtime
    if _CATALOG_CACHE is None or mtime != _CATALOG_MTIME:
        with _CATALOG_PATH.open("r", encoding="utf-8") as fh:
            _CATALOG_CACHE = json.load(fh)
        _CATALOG_MTIME = mtime
    return _CATALOG_CACHE


def _get_current_user(request: Request) -> str:
    """Verify the Bearer JWT and return the user_id.

    Imports `get_current_user` lazily to avoid circular imports with
    `api.py` (which includes this router on app startup).
    """
    from api import get_current_user  # lazy

    return get_current_user(request.headers.get("authorization"))


def _redact_server_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Strip secret_blob; surface a has_secret flag instead.

    The UI never needs the raw secret back — only to know whether one's set
    (so it can show "Replace secret" vs. "Add secret").
    """
    redacted = dict(row)
    has = bool(redacted.pop("secret_blob", None))
    redacted["has_secret"] = has
    # JSONB fields come back as dicts already in psycopg3; normalise None.
    redacted.setdefault("args_json", None)
    redacted.setdefault("tools_json", None)
    return redacted


@router.get("/catalog")
def get_catalog(request: Request) -> Dict[str, Any]:
    """Return the shipped catalog of approved MCP servers.

    Auth required (so we can be a bit louder about who's poking around in
    the future), but the returned data is the same for every user.
    """
    _get_current_user(request)  # auth-gate
    return _load_catalog()


@router.get("/servers")
def list_servers(request: Request) -> List[Dict[str, Any]]:
    """Return the caller's saved MCP server configurations.

    Includes both catalog-derived rows (where `catalog_slug` is set) and
    custom rows (where `is_custom=true`). Secret material is redacted.
    """
    user_id = _get_current_user(request)
    pool = request.app.state.pool
    with pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT id::text, user_id::text, catalog_slug, is_custom, name,
                   enabled, transport, command, args_json, endpoint_url,
                   auth_kind, auth_header, secret_blob, tools_json,
                   last_connected_at, last_error, created_at, updated_at
              FROM mcp_servers
             WHERE user_id = %s
             ORDER BY created_at ASC
            """,
            (user_id,),
        ).fetchall()
        cols = [
            "id", "user_id", "catalog_slug", "is_custom", "name",
            "enabled", "transport", "command", "args_json", "endpoint_url",
            "auth_kind", "auth_header", "secret_blob", "tools_json",
            "last_connected_at", "last_error", "created_at", "updated_at",
        ]
    return [_redact_server_row(dict(zip(cols, r))) for r in rows]


# ── Mutation endpoints (Slice B) ────────────────────────────────────────


def _catalog_entry_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    for entry in _load_catalog().get("entries", []):
        if entry.get("slug") == slug:
            return entry
    return None


def _publish_mcp_updated(user_id: str) -> None:
    """Best-effort SSE nudge so any open MCPs page re-fetches."""
    try:
        from event_bus import bus

        bus.publish(f"user:{user_id}", {"type": "mcp_updated"})
    except Exception:
        pass


# Allowed values per the DB CHECK constraints.
_VALID_TRANSPORTS = {"stdio", "streamable_http", "sse", "http"}
_VALID_AUTH = {"none", "bearer", "api_key_header", "api_key_env", "oauth"}


class CreateServerRequest(BaseModel):
    """Either pick a catalog entry by slug (and we fill defaults from it),
    or define every field ourselves for a custom MCP."""

    catalog_slug: Optional[str] = Field(
        None, description="Slug from /mcp/catalog. Omit for custom MCPs."
    )
    name: Optional[str] = Field(None, max_length=128)
    transport: Optional[str] = None
    command: Optional[str] = None
    args: Optional[List[str]] = None
    endpoint_url: Optional[str] = None
    auth_kind: Optional[str] = None
    auth_header: Optional[str] = None
    # Bearer / api_key_header → single token. api_key_env → key→value map.
    secret: Optional[str] = None
    secret_env: Optional[Dict[str, str]] = None
    enabled: bool = False


class PatchServerRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=128)
    enabled: Optional[bool] = None
    transport: Optional[str] = None
    command: Optional[str] = None
    args: Optional[List[str]] = None
    endpoint_url: Optional[str] = None
    auth_kind: Optional[str] = None
    auth_header: Optional[str] = None
    secret: Optional[str] = None
    secret_env: Optional[Dict[str, str]] = None
    # Explicit clear — patch with `clear_secret: true` to remove any saved
    # token (e.g. before disabling).
    clear_secret: Optional[bool] = None


def _encrypted_blob_from_request(
    auth_kind: Optional[str],
    secret: Optional[str],
    secret_env: Optional[Dict[str, str]],
) -> Optional[str]:
    """Convert the UI's plaintext secret into stored ciphertext.

    Returns None when the caller didn't pass anything secret-related, so
    PATCH can use it as a "leave the existing secret alone" signal.
    """
    from mcp_client import encrypt_env_map, encrypt_secret

    if auth_kind in ("bearer", "api_key_header"):
        if secret is None:
            return None
        if secret == "":
            return ""  # explicit clear
        return encrypt_secret(secret)
    if auth_kind == "api_key_env":
        if secret_env is None:
            return None
        if not secret_env:
            return ""
        return encrypt_env_map(secret_env)
    return None


@router.post("/servers")
def create_server(req: CreateServerRequest, request: Request) -> Dict[str, Any]:
    user_id = _get_current_user(request)
    pool = request.app.state.pool

    # Fill defaults from catalog when slug is provided. Custom rows must
    # supply transport + command/url themselves.
    catalog: Optional[Dict[str, Any]] = None
    if req.catalog_slug:
        catalog = _catalog_entry_by_slug(req.catalog_slug)
        if catalog is None:
            raise HTTPException(404, f"Unknown catalog slug: {req.catalog_slug}")

    transport = (req.transport
                 or (catalog.get("default_transport") if catalog else None))
    if transport not in _VALID_TRANSPORTS:
        raise HTTPException(400, f"Invalid transport: {transport!r}")

    name = (req.name or (catalog.get("name") if catalog else None)
            or req.catalog_slug or "Custom MCP").strip()
    if not name:
        raise HTTPException(400, "name is required")

    command: Optional[str] = req.command
    args: Optional[List[str]] = req.args
    endpoint_url: Optional[str] = req.endpoint_url
    auth_kind: Optional[str] = req.auth_kind
    auth_header: Optional[str] = req.auth_header

    if catalog:
        tcfg = catalog.get("transports", {}).get(transport, {})
        if transport == "stdio":
            command = command or tcfg.get("command")
            if args is None:
                args = list(tcfg.get("args") or [])
        else:
            endpoint_url = endpoint_url or tcfg.get("url_template") or None
            auth_kind = auth_kind or tcfg.get("auth") or catalog.get("auth")
            auth_header = auth_header or tcfg.get("auth_header")
        if not auth_kind:
            auth_kind = catalog.get("auth")

    # Final sanity per transport.
    from mcp_security import (
        MAX_MCPS_PER_USER,
        MCPValidationError,
        validate_endpoint_url,
        validate_stdio,
    )

    if transport == "stdio":
        if not command:
            raise HTTPException(400, "stdio MCPs require a `command`")
        try:
            command, args = validate_stdio(command, list(args or []))
        except MCPValidationError as e:
            raise HTTPException(400, str(e))
    else:
        if not endpoint_url:
            raise HTTPException(
                400, f"{transport} MCPs require an `endpoint_url`"
            )
        try:
            validate_endpoint_url(
                endpoint_url, allow_catalog_domain=bool(req.catalog_slug)
            )
        except MCPValidationError as e:
            raise HTTPException(400, str(e))

    if auth_kind and auth_kind not in _VALID_AUTH:
        raise HTTPException(400, f"Invalid auth_kind: {auth_kind!r}")

    # Per-user concurrency cap — counts existing rows (enabled or not),
    # since each one is a potential active subprocess if toggled on.
    with pool.connection() as conn:
        count = conn.execute(
            "SELECT count(*) FROM mcp_servers WHERE user_id = %s",
            (user_id,),
        ).fetchone()[0]
    if count >= MAX_MCPS_PER_USER:
        raise HTTPException(
            429,
            f"MCP cap reached ({MAX_MCPS_PER_USER}). Remove one before adding "
            "another.",
        )

    secret_blob = _encrypted_blob_from_request(
        auth_kind, req.secret, req.secret_env
    )

    is_custom = req.catalog_slug is None

    with pool.connection() as conn:
        # Upsert on (user_id, catalog_slug) for catalog rows; insert fresh
        # for custom rows (catalog_slug is null and the unique constraint
        # treats nulls as distinct).
        if req.catalog_slug:
            existing = conn.execute(
                "SELECT id FROM mcp_servers WHERE user_id = %s AND catalog_slug = %s",
                (user_id, req.catalog_slug),
            ).fetchone()
            if existing:
                raise HTTPException(
                    409,
                    "This MCP is already configured. PATCH it instead.",
                )
        row = conn.execute(
            """
            INSERT INTO mcp_servers
                (user_id, catalog_slug, is_custom, name, enabled, transport,
                 command, args_json, endpoint_url, auth_kind, auth_header,
                 secret_blob)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id::text
            """,
            (
                user_id,
                req.catalog_slug,
                is_custom,
                name,
                bool(req.enabled),
                transport,
                command,
                json.dumps(args) if args else None,
                endpoint_url,
                auth_kind,
                auth_header,
                secret_blob if secret_blob else None,
            ),
        ).fetchone()
    _publish_mcp_updated(user_id)
    return {"id": row[0]}


@router.patch("/servers/{server_id}")
def patch_server(
    server_id: str,
    req: PatchServerRequest,
    request: Request,
) -> Dict[str, Any]:
    user_id = _get_current_user(request)
    pool = request.app.state.pool

    if req.transport is not None and req.transport not in _VALID_TRANSPORTS:
        raise HTTPException(400, f"Invalid transport: {req.transport!r}")
    if req.auth_kind is not None and req.auth_kind not in _VALID_AUTH:
        raise HTTPException(400, f"Invalid auth_kind: {req.auth_kind!r}")

    # Re-validate transport-shape if any related field changed. We pull
    # the existing row when the patch is partial so unchanged fields
    # still take part in validation.
    from mcp_security import (
        MCPValidationError,
        validate_endpoint_url,
        validate_stdio,
    )

    touches_shape = any(
        v is not None
        for v in (
            req.transport, req.command, req.args, req.endpoint_url,
        )
    )
    if touches_shape:
        with pool.connection() as conn:
            existing = conn.execute(
                """
                SELECT catalog_slug, transport, command, args_json, endpoint_url
                  FROM mcp_servers
                 WHERE id = %s AND user_id = %s
                """,
                (server_id, user_id),
            ).fetchone()
        if not existing:
            raise HTTPException(404, "MCP server not found")
        cat_slug, exist_transport, exist_command, exist_args, exist_url = existing
        effective_transport = req.transport or exist_transport
        effective_command = req.command if req.command is not None else exist_command
        effective_args = (
            req.args if req.args is not None else (exist_args or [])
        )
        if isinstance(effective_args, str):
            try:
                effective_args = json.loads(effective_args)
            except Exception:
                effective_args = []
        effective_url = (
            req.endpoint_url if req.endpoint_url is not None else exist_url
        )
        try:
            if effective_transport == "stdio":
                if not effective_command:
                    raise MCPValidationError("stdio command is required.")
                validate_stdio(effective_command, list(effective_args or []))
            else:
                if not effective_url:
                    raise MCPValidationError(
                        f"{effective_transport} MCPs require an endpoint_url."
                    )
                validate_endpoint_url(
                    effective_url, allow_catalog_domain=bool(cat_slug)
                )
        except MCPValidationError as e:
            raise HTTPException(400, str(e))

    sets: List[str] = []
    vals: List[Any] = []
    if req.name is not None:
        sets.append("name = %s")
        vals.append(req.name.strip())
    if req.enabled is not None:
        sets.append("enabled = %s")
        vals.append(bool(req.enabled))
    if req.transport is not None:
        sets.append("transport = %s")
        vals.append(req.transport)
    if req.command is not None:
        sets.append("command = %s")
        vals.append(req.command)
    if req.args is not None:
        sets.append("args_json = %s")
        vals.append(json.dumps(req.args))
    if req.endpoint_url is not None:
        sets.append("endpoint_url = %s")
        vals.append(req.endpoint_url)
    if req.auth_kind is not None:
        sets.append("auth_kind = %s")
        vals.append(req.auth_kind)
    if req.auth_header is not None:
        sets.append("auth_header = %s")
        vals.append(req.auth_header)

    # Secret handling — only touch secret_blob when explicit.
    if req.clear_secret:
        sets.append("secret_blob = %s")
        vals.append(None)
    elif req.secret is not None or req.secret_env is not None:
        # Need to know the auth_kind to encode the right thing. Take the
        # patched value if provided, else look up the row's current value.
        auth_kind = req.auth_kind
        if auth_kind is None:
            with pool.connection() as conn:
                cur = conn.execute(
                    "SELECT auth_kind FROM mcp_servers WHERE id = %s AND user_id = %s",
                    (server_id, user_id),
                ).fetchone()
                if not cur:
                    raise HTTPException(404, "MCP server not found")
                auth_kind = cur[0]
        blob = _encrypted_blob_from_request(auth_kind, req.secret, req.secret_env)
        if blob is not None:
            sets.append("secret_blob = %s")
            vals.append(blob if blob else None)

    if not sets:
        raise HTTPException(400, "Nothing to update")

    sets.append("updated_at = now()")
    vals.extend([server_id, user_id])

    with pool.connection() as conn:
        cur = conn.execute(
            f"""
            UPDATE mcp_servers
               SET {", ".join(sets)}
             WHERE id = %s AND user_id = %s
             RETURNING id::text
            """,
            tuple(vals),
        ).fetchone()
        if not cur:
            raise HTTPException(404, "MCP server not found")

    _publish_mcp_updated(user_id)
    return {"id": server_id}


@router.post("/servers/{server_id}/test")
async def test_server(server_id: str, request: Request) -> Dict[str, Any]:
    """Connect to an MCP server and list its tools.

    On success the result is also persisted to `tools_json` /
    `last_connected_at`. On failure we record `last_error` so the UI can
    show why the last attempt failed.
    """
    import asyncio

    from mcp_client import discover_tools_for_row

    user_id = _get_current_user(request)
    pool = request.app.state.pool
    with pool.connection() as conn:
        row = conn.execute(
            """
            SELECT id::text, user_id::text, catalog_slug, is_custom, name,
                   enabled, transport, command, args_json, endpoint_url,
                   auth_kind, auth_header, secret_blob
              FROM mcp_servers
             WHERE id = %s AND user_id = %s
            """,
            (server_id, user_id),
        ).fetchone()
    if not row:
        raise HTTPException(404, "MCP server not found")
    cols = [
        "id", "user_id", "catalog_slug", "is_custom", "name",
        "enabled", "transport", "command", "args_json", "endpoint_url",
        "auth_kind", "auth_header", "secret_blob",
    ]
    row_dict = dict(zip(cols, row))

    # Cap discovery — a misbehaving HTTP MCP shouldn't hang the request.
    try:
        tools, err = await asyncio.wait_for(
            discover_tools_for_row(row_dict), timeout=15.0
        )
    except asyncio.TimeoutError:
        tools, err = [], "Timed out after 15s"
    except Exception as e:
        tools, err = [], f"{type(e).__name__}: {str(e)[:240]}"

    with pool.connection() as conn:
        if err:
            conn.execute(
                """
                UPDATE mcp_servers
                   SET last_error = %s, updated_at = now()
                 WHERE id = %s AND user_id = %s
                """,
                (err, server_id, user_id),
            )
        else:
            conn.execute(
                """
                UPDATE mcp_servers
                   SET tools_json = %s,
                       last_connected_at = now(),
                       last_error = NULL,
                       updated_at = now()
                 WHERE id = %s AND user_id = %s
                """,
                (json.dumps(tools), server_id, user_id),
            )
    _publish_mcp_updated(user_id)
    if err:
        return {"ok": False, "tools": [], "error": err}
    return {"ok": True, "tools": tools, "error": None}


@router.delete("/servers/{server_id}")
def delete_server(server_id: str, request: Request) -> Dict[str, Any]:
    """Delete a custom server. For catalog-derived rows, disable + clear
    secret instead (so the user can re-enable without re-entering keys
    is_custom is False — the row stays but goes back to the catalog
    defaults). Behaviour:
      - is_custom=true  → hard DELETE
      - is_custom=false → enabled=false, secret_blob=null
    """
    user_id = _get_current_user(request)
    pool = request.app.state.pool
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT is_custom FROM mcp_servers WHERE id = %s AND user_id = %s",
            (server_id, user_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "MCP server not found")
        if row[0]:
            conn.execute(
                "DELETE FROM mcp_servers WHERE id = %s AND user_id = %s",
                (server_id, user_id),
            )
        else:
            conn.execute(
                """
                UPDATE mcp_servers
                   SET enabled = false, secret_blob = NULL, updated_at = now()
                 WHERE id = %s AND user_id = %s
                """,
                (server_id, user_id),
            )
    _publish_mcp_updated(user_id)
    return {"ok": True}
