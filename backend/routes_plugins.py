"""Plugin inventory endpoints.

A plugin adds a real tool to the agent (see plugins_catalog.py). Endpoints:

  - GET   /plugins          — the catalog merged with the caller's enabled state.
  - PATCH /plugins/{slug}    — enable/disable a plugin for the caller.

Plugins are code-defined (no custom plugins), so a per-user row in the
``plugins`` table just tracks ``enabled`` for a catalog slug.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from plugins_catalog import CATALOG, catalog_entry

router = APIRouter(prefix="/plugins", tags=["plugins"])


def _get_current_user(request: Request) -> str:
    from api import get_current_user  # lazy, avoids circular import

    return get_current_user(request.headers.get("authorization"))


class PluginPatch(BaseModel):
    enabled: bool


@router.get("")
def list_plugins(request: Request) -> List[Dict[str, Any]]:
    """Return the plugin catalog, each merged with the caller's enabled state."""
    user_id = _get_current_user(request)
    pool = request.app.state.pool
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT catalog_slug, enabled FROM plugins WHERE user_id = %s",
            (user_id,),
        ).fetchall()
    enabled_map = {slug: bool(en) for slug, en in rows}

    out: List[Dict[str, Any]] = []
    for entry in CATALOG:
        out.append(
            {
                "slug": entry["slug"],
                "name": entry["name"],
                "publisher": entry.get("publisher", "Built-in"),
                "description": entry["description"],
                "icon": entry.get("icon", "code"),
                "tools": entry.get("tools", []),
                "enabled": enabled_map.get(entry["slug"], False),
            }
        )
    return out


@router.patch("/{slug}")
def patch_plugin(slug: str, body: PluginPatch, request: Request) -> Dict[str, Any]:
    """Enable or disable a plugin for the caller (upserts the tracking row)."""
    user_id = _get_current_user(request)
    if catalog_entry(slug) is None:
        raise HTTPException(404, f"Unknown plugin: {slug}")
    pool = request.app.state.pool
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO plugins (user_id, catalog_slug, enabled)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, catalog_slug)
            DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = now()
            """,
            (user_id, slug, body.enabled),
        )
    return {"slug": slug, "enabled": body.enabled}
