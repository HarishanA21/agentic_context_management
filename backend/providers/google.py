"""Google AI Studio (Gemini API) adapter.

Uses `langchain-google-genai`'s `ChatGoogleGenerativeAI`. This is the
generative-language.googleapis.com endpoint, NOT Vertex AI (which uses
GCP service-account auth instead of API keys — separate adapter someday).
"""

from __future__ import annotations

import os
from typing import Any

import requests
from langchain_core.language_models.chat_models import BaseChatModel

from providers.base import CredentialField, LLMProvider, ProviderTestError


_DEFAULT_MAX_TOKENS = int(os.getenv("CHAT_MAX_TOKENS", "4096"))
_DEFAULT_TEMPERATURE = float(os.getenv("CHAT_TEMPERATURE", "0.3"))
_GENAI_BASE = "https://generativelanguage.googleapis.com/v1beta"


class GoogleAIProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(
            slug="google",
            label="Google AI Studio",
            description=(
                "Gemini family (1.5, 2.0, 2.5). Use the API key from "
                "https://aistudio.google.com/apikey. For Vertex AI / GCP "
                "service accounts, use a different provider (coming later)."
            ),
            supports_model_listing=True,
            suggested_models=[
                "gemini-2.5-pro",
                "gemini-2.5-flash",
                "gemini-2.0-flash",
                "gemini-2.0-flash-exp",
                "gemini-1.5-pro",
                "gemini-1.5-flash",
            ],
            credential_fields=[
                CredentialField(
                    name="api_key",
                    label="API key",
                    placeholder="AIza...",
                    help_text="Get one at https://aistudio.google.com/apikey",
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
            raise ProviderTestError("Google AI api_key is empty.")
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError:
            raise ProviderTestError(
                "langchain-google-genai is not installed. Run "
                "`pip install -r backend/requirements.txt` and restart."
            )
        return ChatGoogleGenerativeAI(
            model=model_id,
            google_api_key=api_key,
            max_output_tokens=runtime.get("max_tokens", _DEFAULT_MAX_TOKENS),
            temperature=runtime.get("temperature", _DEFAULT_TEMPERATURE),
        )

    def test_credentials(
        self, model_id: str, credentials: dict[str, str]
    ) -> None:
        api_key = credentials.get("api_key", "")
        if not api_key:
            raise ProviderTestError("API key is required.")
        # /v1beta/models is the cheapest authenticated GET. Free.
        try:
            r = requests.get(
                f"{_GENAI_BASE}/models",
                params={"key": api_key},
                timeout=8,
            )
        except requests.RequestException as e:
            raise ProviderTestError(f"Could not reach Google: {e}")
        if r.status_code == 400:
            # Google returns 400 with INVALID_ARGUMENT for bad keys.
            body = r.text.lower()
            if "api key not valid" in body or "invalid_argument" in body:
                raise ProviderTestError("Google rejected the API key.")
        if r.status_code == 403:
            raise ProviderTestError(
                "Google returned 403. Enable the Generative Language API "
                "for this key, or use a key created via aistudio.google.com."
            )
        if r.status_code >= 400:
            raise ProviderTestError(
                f"Google returned {r.status_code}: {r.text[:200]}"
            )
        # If a model was specified, verify it's listed.
        if model_id:
            ids = self._extract_chat_models(r.json())
            # Google list uses "models/gemini-2.5-pro" format; user may type
            # either bare ("gemini-2.5-pro") or full — handle both.
            wants = {model_id, f"models/{model_id}"}
            if not (ids & wants):
                raise ProviderTestError(
                    f"Model {model_id!r} isn't available to this key. "
                    f"Try one of: {sorted({i.removeprefix('models/') for i in ids})[:6]}…"
                )

    def list_models(self, credentials: dict[str, str]) -> list[str]:
        api_key = credentials.get("api_key", "")
        if not api_key:
            return []
        try:
            r = requests.get(
                f"{_GENAI_BASE}/models", params={"key": api_key}, timeout=8
            )
            r.raise_for_status()
        except Exception:
            return []
        ids = self._extract_chat_models(r.json())
        return sorted(i.removeprefix("models/") for i in ids)

    @staticmethod
    def _extract_chat_models(payload: dict[str, Any]) -> set[str]:
        out: set[str] = set()
        for m in payload.get("models") or []:
            name = m.get("name", "")
            methods = set(m.get("supportedGenerationMethods") or [])
            if name and "generateContent" in methods:
                out.add(name)
        return out
