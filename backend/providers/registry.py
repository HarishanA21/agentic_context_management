"""Provider registry and per-user active-model resolution.

`PROVIDERS` is the static dict of all supported adapters, looked up by slug.
`resolve_active_model(conn, user_id)` returns the LangChain BaseChatModel the
user has chosen as their default, or falls back to the env-var path
(legacy single-OpenRouter setup) when the user has nothing configured.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from providers.anthropic import AnthropicProvider
from providers.azure import AzureOpenAIProvider
from providers.base import LLMProvider, decrypt_credentials
from providers.bedrock import BedrockProvider
from providers.google import GoogleAIProvider
from providers.openai import OpenAIProvider
from providers.openrouter import OpenRouterProvider


# ── Catalog ─────────────────────────────────────────────────────────────────

PROVIDERS: dict[str, LLMProvider] = {
    "openrouter": OpenRouterProvider(),
    "openai": OpenAIProvider(),
    "anthropic": AnthropicProvider(),
    "bedrock": BedrockProvider(),
    "azure": AzureOpenAIProvider(),
    "google": GoogleAIProvider(),
}


def get_provider(slug: str) -> LLMProvider | None:
    return PROVIDERS.get(slug)


# ── Cache for built chat models ─────────────────────────────────────────────
#
# Building a ChatOpenAI / ChatAnthropic instance is cheap, but doing it on
# every /chat turn churns garbage. Cache by (user_id, provider_id, model_id)
# so a stable choice reuses the same client.
#
# Cache invalidation: when the user edits a provider's credentials, the row's
# `updated_at` changes — we mix it into the cache key so stale clients age
# out naturally.

@lru_cache(maxsize=256)
def _build_cached(
    provider_slug: str,
    model_id: str,
    credentials_repr: tuple[tuple[str, str], ...],
    _updated_at: str,  # part of the cache key; not used inside the body
) -> BaseChatModel:
    provider = PROVIDERS.get(provider_slug)
    if provider is None:
        raise ValueError(f"Unknown provider slug: {provider_slug}")
    return provider.build_chat_model(model_id, dict(credentials_repr))


def _build_for_user(
    provider_slug: str,
    model_id: str,
    credentials: dict[str, str],
    updated_at: Any,
) -> BaseChatModel:
    # dicts aren't hashable; flatten to a sorted tuple-of-tuples for the cache.
    cred_repr = tuple(sorted(credentials.items()))
    return _build_cached(
        provider_slug, model_id, cred_repr, str(updated_at) if updated_at else ""
    )


# ── Active-model resolution ────────────────────────────────────────────────


def _env_fallback_model() -> BaseChatModel:
    """The pre-providers code path: a single OpenRouter ChatOpenAI built from
    env vars. Used when a user has zero configured providers so chat keeps
    working out of the box."""
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    model_id = os.getenv("CHAT_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
    if not api_key:
        # No key, no providers — let the caller error out clearly.
        raise RuntimeError(
            "No LLM providers configured for this user and OPENROUTER_API_KEY "
            "is not set. Configure a provider in Settings."
        )
    # Reuse the OpenRouter adapter so the runtime path is identical.
    return PROVIDERS["openrouter"].build_chat_model(
        model_id, {"api_key": api_key}
    )


def resolve_active_model(conn, user_id: str) -> BaseChatModel:
    """Return the chat model the agent should use for this user.

    Lookup order:
      1. The user's default provider (`is_default = true`).
      2. Env-var fallback (legacy OpenRouter setup).
    """
    row = conn.execute(
        """
        SELECT slug, model_id, credentials_blob, updated_at
        FROM llm_providers
        WHERE user_id = %s AND is_default = true
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()
    if not row:
        return _env_fallback_model()
    slug, model_id, blob, updated_at = row
    credentials = decrypt_credentials(blob or "")
    if not credentials:
        # Encrypted blob unreadable — fall back rather than crash.
        return _env_fallback_model()
    return _build_for_user(slug, model_id, credentials, updated_at)


def resolve_session_model(
    conn, user_id: str, _session_id: str
) -> BaseChatModel:
    """Phase F hook: a session can override the user-level default. For now
    this just delegates to `resolve_active_model` until the per-session
    column lands; the signature is here so /chat doesn't have to change
    when Phase F ships."""
    return resolve_active_model(conn, user_id)
