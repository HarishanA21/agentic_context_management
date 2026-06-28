"""CRUD for context-management profiles.

Built-in presets (user_id IS NULL) appear in GET responses but cannot
be edited or deleted. User-owned profiles support full CRUD.

Mirrors the lazy-import + raw Request pattern used by routes_mcp.py
and routes_providers.py to avoid circular imports with api.py.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from context_profiles import (
    Profile,
    get_profile_by_id,
    list_profiles_for_user,
    parse_profile,
)


router = APIRouter()


MAX_PROFILES_PER_USER = 25


def _auth(request: Request) -> str:
    """Validate Bearer JWT, return user_id. Lazy-imports the verifier
    to avoid circular import with api.py."""
    from api import get_current_user  # lazy

    return get_current_user(request.headers.get("authorization"))


# ─── request bodies ────────────────────────────────────────────────────


class CreateProfileBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    body: Dict[str, Any]
    is_default: bool = False


class UpdateProfileBody(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    body: Optional[Dict[str, Any]] = None
    is_default: Optional[bool] = None


# ─── endpoints ─────────────────────────────────────────────────────────


@router.get("/context/profiles")
def list_profiles(request: Request):
    user_id = _auth(request)
    from api import app  # lazy

    with app.state.pool.connection() as conn:
        return {"profiles": list_profiles_for_user(conn, user_id)}


@router.get("/context/profiles/{profile_id}")
def get_profile(profile_id: str, request: Request):
    user_id = _auth(request)
    from api import app  # lazy

    with app.state.pool.connection() as conn:
        row = get_profile_by_id(conn, user_id, profile_id)
    if row is None:
        raise HTTPException(404, "profile not found")
    return row


@router.post("/context/profiles")
def create_profile(body: CreateProfileBody, request: Request):
    user_id = _auth(request)
    from api import app  # lazy

    # Validate the JSON body against the Profile schema before we touch
    # the DB — gives the user a clean 400 on shape errors.
    try:
        parsed = parse_profile(body.body)
    except Exception as e:
        raise HTTPException(400, f"invalid profile body: {e}")

    with app.state.pool.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM context_profiles WHERE user_id = %s",
            (user_id,),
        ).fetchone()[0]
        if count >= MAX_PROFILES_PER_USER:
            raise HTTPException(
                429,
                f"profile cap reached ({MAX_PROFILES_PER_USER}); delete one first",
            )
        # Name collision against own profiles?
        clash = conn.execute(
            "SELECT 1 FROM context_profiles WHERE user_id = %s AND name = %s",
            (user_id, body.name),
        ).fetchone()
        if clash is not None:
            raise HTTPException(409, f"profile name {body.name!r} already exists")
        # If is_default, clear the flag on every other profile this user owns.
        if body.is_default:
            conn.execute(
                "UPDATE context_profiles SET is_default = false, updated_at = NOW() "
                "WHERE user_id = %s AND is_default = true",
                (user_id,),
            )
        row = conn.execute(
            """
            INSERT INTO context_profiles (user_id, name, body, is_default)
            VALUES (%s, %s, %s::jsonb, %s)
            RETURNING id::text
            """,
            (user_id, body.name, json.dumps(parsed.model_dump()), body.is_default),
        ).fetchone()
        new_id = row[0] if row else None
        return get_profile_by_id(conn, user_id, new_id)


@router.patch("/context/profiles/{profile_id}")
def update_profile(profile_id: str, body: UpdateProfileBody, request: Request):
    user_id = _auth(request)
    from api import app  # lazy

    with app.state.pool.connection() as conn:
        # Built-ins refuse edits.
        owned = conn.execute(
            "SELECT user_id FROM context_profiles WHERE id = %s",
            (profile_id,),
        ).fetchone()
        if owned is None:
            raise HTTPException(404, "profile not found")
        if owned[0] is None:
            raise HTTPException(403, "built-in presets cannot be edited")
        if str(owned[0]) != user_id:
            raise HTTPException(404, "profile not found")

        # Validate new body shape if present.
        if body.body is not None:
            try:
                parsed = parse_profile(body.body)
            except Exception as e:
                raise HTTPException(400, f"invalid profile body: {e}")
            new_body_json: Optional[str] = json.dumps(parsed.model_dump())
        else:
            new_body_json = None

        # Name collision against own *other* profiles.
        if body.name is not None:
            clash = conn.execute(
                "SELECT 1 FROM context_profiles "
                "WHERE user_id = %s AND name = %s AND id != %s",
                (user_id, body.name, profile_id),
            ).fetchone()
            if clash is not None:
                raise HTTPException(409, f"profile name {body.name!r} already exists")

        if body.is_default is True:
            conn.execute(
                "UPDATE context_profiles SET is_default = false, updated_at = NOW() "
                "WHERE user_id = %s AND is_default = true AND id != %s",
                (user_id, profile_id),
            )

        # Build the partial UPDATE.
        sets: List[str] = []
        params: List[Any] = []
        if body.name is not None:
            sets.append("name = %s")
            params.append(body.name)
        if new_body_json is not None:
            sets.append("body = %s::jsonb")
            params.append(new_body_json)
        if body.is_default is not None:
            sets.append("is_default = %s")
            params.append(body.is_default)
        if not sets:
            return get_profile_by_id(conn, user_id, profile_id)
        sets.append("updated_at = NOW()")
        params.append(profile_id)
        conn.execute(
            f"UPDATE context_profiles SET {', '.join(sets)} WHERE id = %s",
            tuple(params),
        )
        return get_profile_by_id(conn, user_id, profile_id)


@router.delete("/context/profiles/{profile_id}")
def delete_profile(profile_id: str, request: Request):
    user_id = _auth(request)
    from api import app  # lazy

    with app.state.pool.connection() as conn:
        row = conn.execute(
            "SELECT user_id FROM context_profiles WHERE id = %s",
            (profile_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "profile not found")
        if row[0] is None:
            raise HTTPException(403, "built-in presets cannot be deleted")
        if str(row[0]) != user_id:
            raise HTTPException(404, "profile not found")
        # Set any session pointing at it back to NULL via the ON DELETE
        # SET NULL FK. No app-level cleanup needed.
        conn.execute("DELETE FROM context_profiles WHERE id = %s", (profile_id,))
        return {"ok": True, "id": profile_id}
