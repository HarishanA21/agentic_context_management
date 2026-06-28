"""Multi-provider LLM adapter layer.

Each adapter wraps a provider's LangChain ChatModel and declares the
credential fields it needs. `registry.PROVIDERS` is the canonical list;
`registry.resolve_active_model(conn, user_id)` returns the BaseChatModel
the agent should use for a given user.
"""

from providers.base import CredentialField, LLMProvider, ProviderTestError
from providers.registry import PROVIDERS, resolve_active_model

__all__ = [
    "CredentialField",
    "LLMProvider",
    "ProviderTestError",
    "PROVIDERS",
    "resolve_active_model",
]
