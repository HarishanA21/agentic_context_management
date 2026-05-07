"""Lazy-initialized Supabase Storage client.

The backend uses the service-role key to bypass RLS, then manually scopes
every operation to <user_id>/<session_id>/... based on the JWT-verified
user_id. API endpoints and agent tools both go through this module.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from supabase import Client, create_client


_client: Optional[Client] = None


def _bucket_name() -> str:
    return os.environ.get("SUPABASE_BUCKET", "project-files")


def get_client() -> Client:
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        _client = create_client(url, key)
    return _client


def get_bucket() -> Any:
    return get_client().storage.from_(_bucket_name())


def session_prefix(user_id: str, session_id: str) -> str:
    """Storage key prefix for a session's files."""
    return f"{user_id}/{session_id}"


def file_key(user_id: str, session_id: str, filename: str) -> str:
    """Full storage key for a file in a session."""
    return f"{user_id}/{session_id}/{filename}"


def is_not_found(err: Exception) -> bool:
    msg = str(err).lower()
    return "not found" in msg or "404" in msg or "object not found" in msg
