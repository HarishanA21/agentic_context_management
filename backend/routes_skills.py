"""Skill inventory endpoints.

A skill is a toggleable bundle of instructions injected into the agent's system
prompt when enabled (see ``skills_catalog.py`` for the model). Endpoints:

  - GET    /skills          — merged list: every catalog skill (with the
                               caller's enabled state) + the caller's custom
                               skills.
  - POST   /skills          — create a custom skill.
  - PATCH  /skills/{ref}     — toggle enabled and/or edit a custom skill.
  - DELETE /skills/{ref}     — delete a custom skill, or detach (disable) a
                               catalog skill.

``ref`` is an opaque client key: ``catalog:<slug>`` for built-ins, ``custom:<uuid>``
for user-authored skills. The merged GET hands these back so the UI never has to
juggle the two id spaces itself.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from skills_catalog import CATALOG, catalog_entry

router = APIRouter(prefix="/skills", tags=["skills"])


def _get_current_user(request: Request) -> str:
    """Verify the Bearer JWT and return the user_id (lazy import avoids a
    circular dependency with api.py, which mounts this router)."""
    from api import get_current_user  # lazy

    return get_current_user(request.headers.get("authorization"))


# ── request models ───────────────────────────────────────────────────────────
class SkillCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field("", max_length=2000)
    instructions: str = Field(..., min_length=1, max_length=20000)
    enabled: bool = True


class SkillPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=2000)
    instructions: Optional[str] = Field(None, min_length=1, max_length=20000)
    enabled: Optional[bool] = None


# ── helpers ───────────────────────────────────────────────────────────────────
def _custom_rows(pool, user_id: str) -> List[Dict[str, Any]]:
    with pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT id::text, name, description, instructions, enabled
              FROM skills
             WHERE user_id = %s AND is_custom = TRUE
             ORDER BY created_at
            """,
            (user_id,),
        ).fetchall()
    cols = ["id", "name", "description", "instructions", "enabled"]
    return [dict(zip(cols, r)) for r in rows]


def _catalog_enabled_slugs(pool, user_id: str) -> Dict[str, bool]:
    """Map of catalog_slug -> enabled for the caller's catalog rows."""
    with pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT catalog_slug, enabled
              FROM skills
             WHERE user_id = %s AND is_custom = FALSE AND catalog_slug IS NOT NULL
            """,
            (user_id,),
        ).fetchall()
    return {slug: bool(enabled) for slug, enabled in rows}


# ── routes ────────────────────────────────────────────────────────────────────
@router.get("")
def list_skills(request: Request) -> List[Dict[str, Any]]:
    """Catalog skills (merged with the caller's enabled state) + custom skills."""
    user_id = _get_current_user(request)
    pool = request.app.state.pool

    enabled_map = _catalog_enabled_slugs(pool, user_id)
    out: List[Dict[str, Any]] = []
    for entry in CATALOG:
        out.append(
            {
                "ref": f"catalog:{entry['slug']}",
                "slug": entry["slug"],
                "name": entry["name"],
                "description": entry["description"],
                "instructions": entry["instructions"],
                "icon": entry.get("icon", "spark"),
                "is_builtin": True,
                "is_custom": False,
                "enabled": enabled_map.get(entry["slug"], False),
            }
        )
    for row in _custom_rows(pool, user_id):
        out.append(
            {
                "ref": f"custom:{row['id']}",
                "slug": None,
                "name": row["name"],
                "description": row["description"] or "",
                "instructions": row["instructions"] or "",
                "icon": "spark",
                "is_builtin": False,
                "is_custom": True,
                "enabled": bool(row["enabled"]),
            }
        )
    return out


@router.post("")
def create_skill(body: SkillCreate, request: Request) -> Dict[str, Any]:
    """Create a custom skill owned by the caller."""
    user_id = _get_current_user(request)
    pool = request.app.state.pool
    with pool.connection() as conn:
        row = conn.execute(
            """
            INSERT INTO skills
                (user_id, is_custom, name, description, instructions, enabled)
            VALUES (%s, TRUE, %s, %s, %s, %s)
            RETURNING id::text
            """,
            (
                user_id,
                body.name.strip(),
                body.description.strip(),
                body.instructions,
                body.enabled,
            ),
        ).fetchone()
    return {
        "ref": f"custom:{row[0]}",
        "id": row[0],
        "is_custom": True,
        "is_builtin": False,
        "enabled": body.enabled,
        "name": body.name.strip(),
        "description": body.description.strip(),
        "instructions": body.instructions,
        "icon": "spark",
    }


@router.patch("/{ref}")
def patch_skill(ref: str, body: SkillPatch, request: Request) -> Dict[str, Any]:
    """Toggle enabled and/or edit a skill.

    For a custom skill, any field may be edited. For a catalog skill only
    ``enabled`` is meaningful (content is code-defined) — we upsert the row so
    enabling one for the first time creates its tracking row.
    """
    user_id = _get_current_user(request)
    pool = request.app.state.pool
    kind, _, ident = ref.partition(":")

    if kind == "catalog":
        if catalog_entry(ident) is None:
            raise HTTPException(404, f"Unknown catalog skill: {ident}")
        if body.enabled is None:
            raise HTTPException(400, "Catalog skills only accept an `enabled` toggle.")
        with pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO skills (user_id, catalog_slug, is_custom, name,
                                    description, instructions, enabled)
                VALUES (%s, %s, FALSE, %s, '', '', %s)
                ON CONFLICT (user_id, catalog_slug)
                DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = now()
                """,
                (user_id, ident, ident, body.enabled),
            )
        return {"ref": ref, "enabled": body.enabled}

    if kind == "custom":
        sets: List[str] = []
        params: List[Any] = []
        if body.name is not None:
            sets.append("name = %s")
            params.append(body.name.strip())
        if body.description is not None:
            sets.append("description = %s")
            params.append(body.description.strip())
        if body.instructions is not None:
            sets.append("instructions = %s")
            params.append(body.instructions)
        if body.enabled is not None:
            sets.append("enabled = %s")
            params.append(body.enabled)
        if not sets:
            raise HTTPException(400, "Nothing to update.")
        sets.append("updated_at = now()")
        params.extend([ident, user_id])
        with pool.connection() as conn:
            row = conn.execute(
                f"""
                UPDATE skills SET {', '.join(sets)}
                 WHERE id = %s AND user_id = %s AND is_custom = TRUE
                RETURNING id::text
                """,
                params,
            ).fetchone()
        if row is None:
            raise HTTPException(404, "Skill not found.")
        return {"ref": ref, "ok": True}

    raise HTTPException(400, f"Bad skill ref: {ref}")


@router.delete("/{ref}")
def delete_skill(ref: str, request: Request) -> Dict[str, Any]:
    """Delete a custom skill, or detach (disable) a catalog skill."""
    user_id = _get_current_user(request)
    pool = request.app.state.pool
    kind, _, ident = ref.partition(":")

    with pool.connection() as conn:
        if kind == "catalog":
            conn.execute(
                "DELETE FROM skills WHERE user_id = %s AND catalog_slug = %s "
                "AND is_custom = FALSE",
                (user_id, ident),
            )
        elif kind == "custom":
            row = conn.execute(
                "DELETE FROM skills WHERE id = %s AND user_id = %s "
                "AND is_custom = TRUE RETURNING id",
                (ident, user_id),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Skill not found.")
        else:
            raise HTTPException(400, f"Bad skill ref: {ref}")
    return {"ref": ref, "deleted": True}
