"""LLM provider configuration endpoints.

Per-user multi-provider settings, mirroring the MCP-server CRUD pattern:

  - GET    /providers/catalog        — static list of supported providers + their credential schemas (for the Settings form).
  - GET    /providers                — caller's llm_providers rows, credentials redacted.
  - POST   /providers                — create (encrypts + verifies).
  - PATCH  /providers/{id}           — update (model_id / label / credentials).
  - DELETE /providers/{id}           — remove. Refuses to delete the only row if it's the default (orphans the user).
  - POST   /providers/{id}/test      — re-verify credentials, persist last_error.
  - POST   /providers/{id}/default   — set as the active default (clears the flag on other rows).

Encryption uses the same Fernet helpers as the MCP store (`MCP_SECRET_KEY`).
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from providers.base import (
    ProviderTestError,
    decrypt_credentials,
    encrypt_credentials,
)
from providers.registry import PROVIDERS

router = APIRouter(prefix="/providers", tags=["providers"])

# Hard cap so nobody fills the table by accident.
MAX_PROVIDERS_PER_USER = 10

# Rate limit for /test: token bucket per user, reset every 60s.
_RATE_BUCKET: Dict[str, deque[float]] = {}
_TEST_RATE_WINDOW_SECS = 60
_TEST_RATE_MAX_CALLS = 20


def _get_current_user(request: Request) -> str:
    """Lazy import of api.get_current_user to avoid circular imports."""
    from api import get_current_user

    return get_current_user(request.headers.get("authorization"))


def _pool():
    from api import app

    return app.state.pool


def _rate_limit_test(user_id: str) -> None:
    now = time.monotonic()
    bucket = _RATE_BUCKET.setdefault(user_id, deque())
    while bucket and bucket[0] < now - _TEST_RATE_WINDOW_SECS:
        bucket.popleft()
    if len(bucket) >= _TEST_RATE_MAX_CALLS:
        raise HTTPException(
            429,
            f"Too many test requests — wait {_TEST_RATE_WINDOW_SECS}s.",
        )
    bucket.append(now)


def _row_to_dict(row: tuple) -> Dict[str, Any]:
    """Shape a SELECT result for the API response. Never returns the
    encrypted blob — only a has_credentials boolean."""
    (
        id_,
        slug,
        label,
        model_id,
        credentials_blob,
        is_default,
        last_error,
        last_tested_at,
        created_at,
        updated_at,
    ) = row
    return {
        "id": str(id_),
        "slug": slug,
        "label": label,
        "model_id": model_id,
        "has_credentials": bool(credentials_blob),
        "is_default": is_default,
        "last_error": last_error,
        "last_tested_at": last_tested_at.isoformat() if last_tested_at else None,
        "created_at": created_at.isoformat() if created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


_SELECT_FIELDS = (
    "id, slug, label, model_id, credentials_blob, is_default, "
    "last_error, last_tested_at, created_at, updated_at"
)


# ── Request models ─────────────────────────────────────────────────────────


class CreateProviderRequest(BaseModel):
    slug: str = Field(..., description="Provider slug — must match a registered adapter.")
    label: str = Field(..., min_length=1, max_length=80)
    model_id: str = Field(..., min_length=1, max_length=200)
    credentials: Dict[str, str] = Field(default_factory=dict)
    is_default: bool = False
    # If true, do a test_credentials() before saving and reject on failure.
    verify: bool = True


class UpdateProviderRequest(BaseModel):
    label: Optional[str] = Field(None, min_length=1, max_length=80)
    model_id: Optional[str] = Field(None, min_length=1, max_length=200)
    credentials: Optional[Dict[str, str]] = None
    verify: bool = False


# ── Catalog ────────────────────────────────────────────────────────────────


@router.get("/catalog")
def get_catalog() -> List[Dict[str, Any]]:
    """Static list of supported providers + their credential schemas.
    The Settings UI uses this to render per-provider config forms."""
    out: List[Dict[str, Any]] = []
    for slug, provider in PROVIDERS.items():
        out.append(
            {
                "slug": slug,
                "label": provider.label,
                "description": provider.description,
                "supports_model_listing": provider.supports_model_listing,
                "suggested_models": list(provider.suggested_models),
                "credential_fields": [
                    {
                        "name": f.name,
                        "label": f.label,
                        "secret": f.secret,
                        "required": f.required,
                        "placeholder": f.placeholder,
                        "help_text": f.help_text,
                        "options": list(f.options),
                    }
                    for f in provider.credential_fields
                ],
            }
        )
    return out


# ── CRUD ───────────────────────────────────────────────────────────────────


@router.get("")
def list_providers(request: Request) -> List[Dict[str, Any]]:
    user_id = _get_current_user(request)
    with _pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT {_SELECT_FIELDS}
              FROM llm_providers
             WHERE user_id = %s
             ORDER BY is_default DESC, created_at ASC
            """,
            (user_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.post("")
def create_provider(req: CreateProviderRequest, request: Request) -> Dict[str, Any]:
    user_id = _get_current_user(request)
    provider = PROVIDERS.get(req.slug)
    if provider is None:
        raise HTTPException(400, f"Unknown provider slug: {req.slug}")

    label = req.label.strip()
    model_id = req.model_id.strip()
    if not label or not model_id:
        raise HTTPException(400, "label and model_id are required.")

    with _pool().connection() as conn:
        # Quota check.
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM llm_providers WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        if count >= MAX_PROVIDERS_PER_USER:
            raise HTTPException(
                400,
                f"You already have {MAX_PROVIDERS_PER_USER} providers — delete "
                "one before adding another.",
            )

    # Verify before storing if requested. Cheaper than persisting broken creds.
    last_error: Optional[str] = None
    if req.verify:
        try:
            provider.test_credentials(model_id, req.credentials)
        except ProviderTestError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(400, f"Verification failed: {e}")
        last_error = None

    blob = encrypt_credentials(req.credentials)

    with _pool().connection() as conn:
        if req.is_default:
            conn.execute(
                "UPDATE llm_providers SET is_default = false WHERE user_id = %s",
                (user_id,),
            )
        row = conn.execute(
            f"""
            INSERT INTO llm_providers
                (user_id, slug, label, model_id, credentials_blob, is_default,
                 last_error, last_tested_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            RETURNING {_SELECT_FIELDS}
            """,
            (
                user_id,
                req.slug,
                label,
                model_id,
                blob,
                req.is_default,
                last_error,
            ),
        ).fetchone()
    return _row_to_dict(row)


@router.patch("/{provider_id}")
def update_provider(
    provider_id: str, req: UpdateProviderRequest, request: Request
) -> Dict[str, Any]:
    user_id = _get_current_user(request)
    with _pool().connection() as conn:
        existing = conn.execute(
            f"SELECT {_SELECT_FIELDS} FROM llm_providers "
            "WHERE id = %s AND user_id = %s",
            (provider_id, user_id),
        ).fetchone()
    if not existing:
        raise HTTPException(404, "Provider not found.")
    existing_dict = _row_to_dict(existing)
    slug = existing_dict["slug"]
    provider = PROVIDERS.get(slug)
    if provider is None:
        raise HTTPException(500, f"Stored slug {slug!r} is no longer registered.")

    new_label = (req.label or existing_dict["label"]).strip()
    new_model_id = (req.model_id or existing_dict["model_id"]).strip()
    if not new_label or not new_model_id:
        raise HTTPException(400, "label and model_id cannot be empty.")

    if req.credentials is not None:
        new_credentials = req.credentials
        new_blob = encrypt_credentials(new_credentials)
    else:
        new_credentials = None
        new_blob = None  # leaves existing blob unchanged

    if req.verify:
        # Verify against the resulting (post-update) creds + model_id.
        creds_for_test = (
            new_credentials
            if new_credentials is not None
            else decrypt_credentials(existing[4] or "")
        )
        try:
            provider.test_credentials(new_model_id, creds_for_test)
        except ProviderTestError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(400, f"Verification failed: {e}")

    with _pool().connection() as conn:
        if new_blob is not None:
            row = conn.execute(
                f"""
                UPDATE llm_providers
                   SET label = %s,
                       model_id = %s,
                       credentials_blob = %s,
                       last_error = NULL,
                       last_tested_at = CASE WHEN %s THEN NOW() ELSE last_tested_at END,
                       updated_at = NOW()
                 WHERE id = %s AND user_id = %s
                 RETURNING {_SELECT_FIELDS}
                """,
                (
                    new_label,
                    new_model_id,
                    new_blob,
                    req.verify,
                    provider_id,
                    user_id,
                ),
            ).fetchone()
        else:
            row = conn.execute(
                f"""
                UPDATE llm_providers
                   SET label = %s,
                       model_id = %s,
                       last_tested_at = CASE WHEN %s THEN NOW() ELSE last_tested_at END,
                       updated_at = NOW()
                 WHERE id = %s AND user_id = %s
                 RETURNING {_SELECT_FIELDS}
                """,
                (
                    new_label,
                    new_model_id,
                    req.verify,
                    provider_id,
                    user_id,
                ),
            ).fetchone()
    if not row:
        raise HTTPException(404, "Provider not found.")
    return _row_to_dict(row)


@router.delete("/{provider_id}")
def delete_provider(provider_id: str, request: Request) -> Dict[str, Any]:
    user_id = _get_current_user(request)
    with _pool().connection() as conn:
        deleted = conn.execute(
            "DELETE FROM llm_providers WHERE id = %s AND user_id = %s "
            "RETURNING id",
            (provider_id, user_id),
        ).fetchone()
    if not deleted:
        raise HTTPException(404, "Provider not found.")
    return {"ok": True, "id": str(deleted[0])}


# ── Actions ────────────────────────────────────────────────────────────────


@router.post("/{provider_id}/test")
def test_provider(provider_id: str, request: Request) -> Dict[str, Any]:
    user_id = _get_current_user(request)
    _rate_limit_test(user_id)

    with _pool().connection() as conn:
        row = conn.execute(
            "SELECT slug, model_id, credentials_blob "
            "FROM llm_providers WHERE id = %s AND user_id = %s",
            (provider_id, user_id),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Provider not found.")
    slug, model_id, blob = row
    provider = PROVIDERS.get(slug)
    if provider is None:
        raise HTTPException(500, f"Stored slug {slug!r} is no longer registered.")

    creds = decrypt_credentials(blob or "")
    err: Optional[str] = None
    try:
        provider.test_credentials(model_id, creds)
    except ProviderTestError as e:
        err = str(e)
    except Exception as e:
        err = f"Unexpected error: {e}"

    with _pool().connection() as conn:
        conn.execute(
            "UPDATE llm_providers "
            "SET last_error = %s, last_tested_at = NOW(), updated_at = NOW() "
            "WHERE id = %s AND user_id = %s",
            (err, provider_id, user_id),
        )

    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "error": None}


@router.post("/{provider_id}/default")
def set_default(provider_id: str, request: Request) -> Dict[str, Any]:
    user_id = _get_current_user(request)
    with _pool().connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM llm_providers WHERE id = %s AND user_id = %s",
            (provider_id, user_id),
        ).fetchone()
        if not existing:
            raise HTTPException(404, "Provider not found.")
        # Single atomic flip — clear all then set the target.
        conn.execute(
            "UPDATE llm_providers SET is_default = false, updated_at = NOW() "
            "WHERE user_id = %s",
            (user_id,),
        )
        conn.execute(
            "UPDATE llm_providers SET is_default = true, updated_at = NOW() "
            "WHERE id = %s AND user_id = %s",
            (provider_id, user_id),
        )
    return {"ok": True, "id": provider_id}


@router.post("/{provider_id}/models")
def list_provider_models(provider_id: str, request: Request) -> Dict[str, Any]:
    """Live model list for providers that publish one (OpenAI, OpenRouter,
    Bedrock, Azure, Google). Returns `[]` if the provider doesn't expose
    /models or if the call failed."""
    user_id = _get_current_user(request)
    _rate_limit_test(user_id)  # share the bucket — this hits provider APIs too

    with _pool().connection() as conn:
        row = conn.execute(
            "SELECT slug, credentials_blob FROM llm_providers "
            "WHERE id = %s AND user_id = %s",
            (provider_id, user_id),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Provider not found.")
    slug, blob = row
    provider = PROVIDERS.get(slug)
    if provider is None:
        raise HTTPException(500, f"Stored slug {slug!r} is no longer registered.")
    creds = decrypt_credentials(blob or "")
    try:
        models = provider.list_models(creds)
    except Exception as e:
        return {"models": [], "error": str(e)}
    return {"models": models, "error": None}


class DiscoverModelsRequest(BaseModel):
    slug: str
    credentials: Dict[str, str] = Field(default_factory=dict)


@router.post("/discover-models")
def discover_models(
    req: DiscoverModelsRequest, request: Request
) -> Dict[str, Any]:
    """Live model list given a provider slug + credentials WITHOUT saving
    anything. Used by the Settings 'Add provider' modal so users can pick
    a model from a dropdown before committing the row."""
    user_id = _get_current_user(request)
    _rate_limit_test(user_id)

    provider = PROVIDERS.get(req.slug)
    if provider is None:
        raise HTTPException(400, f"Unknown provider slug: {req.slug}")
    try:
        models = provider.list_models(req.credentials)
    except Exception as e:
        return {"models": [], "error": str(e)}
    return {"models": models, "error": None}
