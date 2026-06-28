"""Anthropic adapter.

Direct access to Claude (Haiku / Sonnet / Opus). Anthropic doesn't expose a
public /models endpoint, so we hardcode the currently-shipping family for
the Settings UI dropdown — the user can still override with a free-text
model ID if they want to try a newer one.
"""

from __future__ import annotations

import os
from typing import Any

import requests
from langchain_core.language_models.chat_models import BaseChatModel

from providers.base import CredentialField, LLMProvider, ProviderTestError


_DEFAULT_MAX_TOKENS = int(os.getenv("CHAT_MAX_TOKENS", "4096"))
_DEFAULT_TEMPERATURE = float(os.getenv("CHAT_TEMPERATURE", "0.3"))
_ANTHROPIC_BASE = "https://api.anthropic.com/v1"

# Known model IDs as of late 2025 / early 2026. Used as the dropdown source
# since Anthropic doesn't publish /models. User can still type any ID.
_KNOWN_MODELS = [
    "claude-haiku-4-5-20251001",
    "claude-haiku-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
]


class AnthropicProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(
            slug="anthropic",
            label="Anthropic",
            description=(
                "Direct API access to Claude (Haiku / Sonnet / Opus). "
                "Requires an Anthropic Console account."
            ),
            supports_model_listing=True,
            suggested_models=list(_KNOWN_MODELS),
            credential_fields=[
                CredentialField(
                    name="api_key",
                    label="API key",
                    placeholder="sk-ant-...",
                    help_text=(
                        "Get one at https://console.anthropic.com/settings/keys"
                    ),
                ),
            ],
        )

    def build_chat_model(
        self,
        model_id: str,
        credentials: dict[str, str],
        **runtime: Any,
    ) -> BaseChatModel:
        # Local import: `langchain-anthropic` is already in requirements.txt,
        # but importing it eagerly would pull in `anthropic` at module load
        # which slows uvicorn startup. Lazy load only when actually used.
        from langchain_anthropic import ChatAnthropic

        api_key = credentials.get("api_key", "")
        if not api_key:
            raise ProviderTestError("Anthropic api_key is empty.")
        return ChatAnthropic(
            model=model_id,
            anthropic_api_key=api_key,
            max_tokens=runtime.get("max_tokens", _DEFAULT_MAX_TOKENS),
            temperature=runtime.get("temperature", _DEFAULT_TEMPERATURE),
            streaming=True,
        )

    def test_credentials(
        self, model_id: str, credentials: dict[str, str]
    ) -> None:
        api_key = credentials.get("api_key", "")
        if not api_key:
            raise ProviderTestError("API key is required.")
        # Anthropic has no public /models endpoint; the cheapest auth check
        # is a 1-token /messages call. Cost: ~1 input + 1 output token.
        try:
            r = requests.post(
                f"{_ANTHROPIC_BASE}/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model_id or "claude-haiku-4-5",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                timeout=10,
            )
        except requests.RequestException as e:
            raise ProviderTestError(f"Could not reach Anthropic: {e}")
        if r.status_code == 401:
            raise ProviderTestError("Anthropic rejected the key (401).")
        if r.status_code == 404:
            raise ProviderTestError(
                f"Anthropic doesn't recognise model {model_id!r}. Check the "
                "model ID — common families: claude-haiku-4-5, claude-sonnet-4-6."
            )
        if r.status_code >= 400:
            # 400 here is usually a malformed model name; surface the body so
            # the user sees "Invalid model" or whatever Anthropic returned.
            raise ProviderTestError(
                f"Anthropic returned {r.status_code}: {r.text[:200]}"
            )

    def list_models(self, _credentials: dict[str, str]) -> list[str]:
        # Anthropic doesn't expose /models; return the hardcoded shortlist
        # (same content as suggested_models). Credentials are ignored.
        return list(_KNOWN_MODELS)
