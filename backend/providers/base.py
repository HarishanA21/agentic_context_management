"""LLMProvider ABC + shared types.

An adapter implements this interface for one LLM provider (OpenAI, Bedrock,
etc.). The adapter only knows how to build a LangChain BaseChatModel from a
credentials dict — it knows nothing about HTTP routes, the database, or the
agent. Storage + auth happens in `routes_providers.py` and `registry.py`.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

# Reuse the Fernet helpers already used for MCP secrets so we don't manage
# two keys. Same `MCP_SECRET_KEY` env protects both stores.
from mcp_client import decrypt_secret, encrypt_secret


class ProviderTestError(Exception):
    """Raised when test_credentials() fails. Message is shown to the user."""


@dataclass
class CredentialField:
    """One input the Settings form should render for a provider."""

    name: str               # JSON key — e.g. "api_key"
    label: str              # human label — "API key"
    secret: bool = True     # render as <input type="password">
    required: bool = True
    placeholder: str = ""
    help_text: str = ""
    # Optional: if non-empty, render as a <select> instead of text input.
    options: list[str] = field(default_factory=list)


@dataclass
class LLMProvider(ABC):
    """One adapter per supported provider. Subclass to add a new one."""

    slug: str       # "openai" | "anthropic" | "openrouter" | ...
    label: str      # "OpenAI"
    credential_fields: list[CredentialField] = field(default_factory=list)
    # If the provider exposes /models, the registry will offer a live picker.
    supports_model_listing: bool = False
    # Free text for the Settings UI to explain what the user is signing up for.
    description: str = ""
    # Curated short-list shown in the Settings dropdown BEFORE the user
    # clicks "Fetch available". Lets users pick popular models without
    # spending a verification round-trip. Free-text typing still works.
    suggested_models: list[str] = field(default_factory=list)

    @abstractmethod
    def build_chat_model(
        self,
        model_id: str,
        credentials: dict[str, str],
        **runtime: Any,
    ) -> BaseChatModel:
        """Construct a LangChain chat model. Called on every /chat turn (cached
        upstream in `registry.py`), so keep it fast — no network calls here."""

    @abstractmethod
    def test_credentials(
        self, model_id: str, credentials: dict[str, str]
    ) -> None:
        """Make the smallest possible authenticated request to verify the
        credentials work. Raise ProviderTestError with a user-friendly message
        on failure. Returns None on success."""

    def list_models(self, _credentials: dict[str, str]) -> list[str]:
        """Optional: return live model IDs. Default = empty (subclasses
        override with a provider-specific implementation)."""
        return []


# ── credential encryption ──────────────────────────────────────────────────


def encrypt_credentials(credentials: dict[str, str]) -> str:
    """Serialize the credentials dict and Fernet-encrypt the result.
    Empty dict → empty string."""
    if not credentials:
        return ""
    return encrypt_secret(json.dumps(credentials))


def decrypt_credentials(blob: str) -> dict[str, str]:
    """Inverse of encrypt_credentials. Empty/invalid → empty dict."""
    if not blob:
        return {}
    try:
        raw = decrypt_secret(blob)
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}
