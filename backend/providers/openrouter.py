"""OpenRouter adapter.

OpenRouter is OpenAI-API-compatible — we use `ChatOpenAI` with a custom
`openai_api_base`. The site exposes /api/v1/models which we can list at
config time.
"""

from __future__ import annotations

import os
from typing import Any

import requests
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from providers.base import CredentialField, LLMProvider, ProviderTestError


# No output cap by default — a hard ceiling clips large tool-call arguments
# (e.g. writing a big file) and reasoning models mid-thought. Set
# CHAT_MAX_TOKENS to bring back a ceiling. Mirrors api._build_model.
_max_env = os.getenv("CHAT_MAX_TOKENS", "").strip()
_DEFAULT_MAX_TOKENS = int(_max_env) if _max_env else None
_DEFAULT_TEMPERATURE = float(os.getenv("CHAT_TEMPERATURE", "0.3"))
_OPENROUTER_BASE = "https://openrouter.ai/api/v1"


class OpenRouterProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(
            slug="openrouter",
            label="OpenRouter",
            description=(
                "Single key, hundreds of models from many providers. Best for "
                "trying many models without juggling separate accounts."
            ),
            supports_model_listing=True,
            suggested_models=[
                "meta-llama/llama-3.3-70b-instruct:free",
                "qwen/qwen-2.5-72b-instruct:free",
                "google/gemini-2.0-flash-exp:free",
                "openai/gpt-4o-mini",
                "anthropic/claude-haiku-4-5",
                "anthropic/claude-sonnet-4-6",
                "inclusionai/ring-2.6-1t:free",
            ],
            credential_fields=[
                CredentialField(
                    name="api_key",
                    label="API key",
                    placeholder="sk-or-v1-...",
                    help_text="Get one at https://openrouter.ai/keys",
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
            raise ProviderTestError("OpenRouter api_key is empty.")
        return ChatOpenAI(
            model=model_id,
            openai_api_key=api_key,
            openai_api_base=_OPENROUTER_BASE,
            max_tokens=runtime.get("max_tokens", _DEFAULT_MAX_TOKENS),
            temperature=runtime.get("temperature", _DEFAULT_TEMPERATURE),
            default_headers={
                "HTTP-Referer": "http://localhost",
                "X-Title": "FYP Agent Project",
            },
        )

    def test_credentials(
        self, _model_id: str, credentials: dict[str, str]
    ) -> None:
        # `/auth/key` is the cheapest authenticated GET — confirms the
        # key parses without consuming any inference quota. It returns the
        # same response regardless of which model the user picked, so
        # model_id is intentionally ignored here.
        api_key = credentials.get("api_key", "")
        if not api_key:
            raise ProviderTestError("API key is required.")
        try:
            r = requests.get(
                f"{_OPENROUTER_BASE}/auth/key",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=5,
            )
        except requests.RequestException as e:
            raise ProviderTestError(f"Could not reach OpenRouter: {e}")
        if r.status_code == 401:
            raise ProviderTestError("OpenRouter rejected the key (401).")
        if r.status_code >= 400:
            raise ProviderTestError(
                f"OpenRouter returned {r.status_code}: {r.text[:160]}"
            )

    def list_models(self, _credentials: dict[str, str]) -> list[str]:
        """OpenRouter's /models endpoint is public — credentials are not
        required. Returns up to ~200 model IDs."""
        try:
            r = requests.get(f"{_OPENROUTER_BASE}/models", timeout=5)
            r.raise_for_status()
            data = r.json()
            return [m["id"] for m in data.get("data", []) if "id" in m]
        except Exception:
            return []
