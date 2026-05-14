"""OpenAI adapter.

The official OpenAI API. Uses LangChain's `ChatOpenAI` (default base URL).
Supports an optional `organization` field for users on multi-org accounts.
"""

from __future__ import annotations

import os
from typing import Any

import requests
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from providers.base import CredentialField, LLMProvider, ProviderTestError


_DEFAULT_MAX_TOKENS_ENV = os.getenv("CHAT_MAX_TOKENS", "").strip()
_DEFAULT_TEMPERATURE = float(os.getenv("CHAT_TEMPERATURE", "0.3"))
_OPENAI_BASE = "https://api.openai.com/v1"


class OpenAIProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(
            slug="openai",
            label="OpenAI",
            description=(
                "Direct API access to GPT-4 / GPT-4o / o-series models. "
                "Requires an OpenAI account with billing enabled."
            ),
            supports_model_listing=True,
            suggested_models=[
                "gpt-4o-mini",
                "gpt-4o",
                "gpt-4.1",
                "gpt-4.1-mini",
                "o4-mini",
                "o3-mini",
            ],
            credential_fields=[
                CredentialField(
                    name="api_key",
                    label="API key",
                    placeholder="sk-...",
                    help_text="Get one at https://platform.openai.com/api-keys",
                ),
                CredentialField(
                    name="organization",
                    label="Organization ID (optional)",
                    secret=False,
                    required=False,
                    placeholder="org-...",
                    help_text="Only needed if your account belongs to multiple orgs.",
                ),
            ],
        )

    def build_chat_model(
        self,
        model_id: str,
        credentials: dict[str, str],
        **runtime: Any,
    ) -> BaseChatModel:
        api_key = credentials.get("api_key", "")
        if not api_key:
            raise ProviderTestError("OpenAI api_key is empty.")
        org = credentials.get("organization", "").strip() or None
        max_tokens = runtime.get("max_tokens")
        if max_tokens is None and _DEFAULT_MAX_TOKENS_ENV:
            max_tokens = int(_DEFAULT_MAX_TOKENS_ENV)
        return ChatOpenAI(
            model=model_id,
            openai_api_key=api_key,
            openai_organization=org,
            max_tokens=max_tokens,
            temperature=runtime.get("temperature", _DEFAULT_TEMPERATURE),
            streaming=True,
        )

    def test_credentials(
        self, _model_id: str, credentials: dict[str, str]
    ) -> None:
        # `/v1/models` is the cheapest authenticated GET and doesn't care
        # which model the user picked — so we skip model_id validation here.
        api_key = credentials.get("api_key", "")
        if not api_key:
            raise ProviderTestError("API key is required.")
        headers = {"Authorization": f"Bearer {api_key}"}
        org = credentials.get("organization", "").strip()
        if org:
            headers["OpenAI-Organization"] = org
        try:
            r = requests.get(f"{_OPENAI_BASE}/models", headers=headers, timeout=5)
        except requests.RequestException as e:
            raise ProviderTestError(f"Could not reach OpenAI: {e}")
        if r.status_code == 401:
            raise ProviderTestError("OpenAI rejected the key (401).")
        if r.status_code == 403:
            raise ProviderTestError(
                "OpenAI returned 403. Check the organization ID or that the "
                "key has access to the API."
            )
        if r.status_code >= 400:
            raise ProviderTestError(
                f"OpenAI returned {r.status_code}: {r.text[:160]}"
            )

    def list_models(self, credentials: dict[str, str]) -> list[str]:
        api_key = credentials.get("api_key", "")
        if not api_key:
            return []
        headers = {"Authorization": f"Bearer {api_key}"}
        org = credentials.get("organization", "").strip()
        if org:
            headers["OpenAI-Organization"] = org
        try:
            r = requests.get(f"{_OPENAI_BASE}/models", headers=headers, timeout=5)
            r.raise_for_status()
        except Exception:
            return []
        ids = [m.get("id", "") for m in (r.json().get("data") or [])]
        # Filter to chat-capable families. OpenAI's /models lists embedding
        # and TTS models too; those would crash on first invoke.
        chat = [
            i
            for i in ids
            if i
            and (
                i.startswith("gpt-")
                or i.startswith("o1")
                or i.startswith("o3")
                or i.startswith("o4")
                or i.startswith("chatgpt-")
            )
        ]
        return sorted(chat)
