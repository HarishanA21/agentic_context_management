"""Azure OpenAI adapter.

Azure exposes OpenAI models as **deployments**. The user names the
deployment in the Azure portal (often the same string as the underlying
model, but it doesn't have to be). Our `model_id` field is the deployment
name; the underlying model isn't passed separately.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

import requests
from langchain_core.language_models.chat_models import BaseChatModel

from providers.base import CredentialField, LLMProvider, ProviderTestError


_DEFAULT_MAX_TOKENS_ENV = os.getenv("CHAT_MAX_TOKENS", "").strip()
_DEFAULT_TEMPERATURE = float(os.getenv("CHAT_TEMPERATURE", "0.3"))
_DEFAULT_API_VERSION = "2024-10-21"


class AzureOpenAIProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(
            slug="azure",
            label="Azure OpenAI",
            description=(
                "OpenAI models hosted on Azure. Note: in Azure you talk to a "
                "deployment name, not a model name — set 'Model ID' below to "
                "your deployment slug from the Azure portal."
            ),
            supports_model_listing=True,
            credential_fields=[
                CredentialField(
                    name="api_key",
                    label="API key",
                    placeholder="(64-char hex)",
                ),
                CredentialField(
                    name="azure_endpoint",
                    label="Endpoint",
                    secret=False,
                    placeholder="https://<resource>.openai.azure.com",
                    help_text="Resource URL from Azure portal → Keys and Endpoint.",
                ),
                CredentialField(
                    name="api_version",
                    label="API version",
                    secret=False,
                    required=False,
                    placeholder=_DEFAULT_API_VERSION,
                    help_text=(
                        f"Leave blank to use {_DEFAULT_API_VERSION}. Older "
                        "versions like 2024-02-15-preview also work."
                    ),
                ),
            ],
        )

    def _check_creds(self, credentials: dict[str, str]) -> tuple[str, str, str]:
        api_key = credentials.get("api_key", "").strip()
        endpoint = credentials.get("azure_endpoint", "").strip().rstrip("/")
        api_version = (
            credentials.get("api_version", "").strip() or _DEFAULT_API_VERSION
        )
        if not api_key or not endpoint:
            raise ProviderTestError(
                "Azure requires api_key and azure_endpoint."
            )
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.netloc.endswith(
            ".openai.azure.com"
        ):
            raise ProviderTestError(
                "Endpoint must be an https URL ending in "
                "'.openai.azure.com' (e.g. https://my-rg.openai.azure.com)."
            )
        return api_key, endpoint, api_version

    def build_chat_model(
        self,
        model_id: str,
        credentials: dict[str, str],
        **runtime: Any,
    ) -> BaseChatModel:
        api_key, endpoint, api_version = self._check_creds(credentials)
        if not model_id:
            raise ProviderTestError(
                "Model ID is required (= your Azure deployment name)."
            )
        # AzureChatOpenAI lives in langchain-openai, already installed.
        from langchain_openai import AzureChatOpenAI

        max_tokens = runtime.get("max_tokens")
        if max_tokens is None and _DEFAULT_MAX_TOKENS_ENV:
            max_tokens = int(_DEFAULT_MAX_TOKENS_ENV)
        return AzureChatOpenAI(
            azure_deployment=model_id,
            azure_endpoint=endpoint,
            api_version=api_version,
            api_key=api_key,
            max_tokens=max_tokens,
            temperature=runtime.get("temperature", _DEFAULT_TEMPERATURE),
            streaming=True,
        )

    def test_credentials(
        self, model_id: str, credentials: dict[str, str]
    ) -> None:
        api_key, endpoint, api_version = self._check_creds(credentials)
        # List deployments is the cheapest authenticated GET — no tokens spent.
        # /openai/deployments?api-version=... returns 200 with the deployment array.
        url = f"{endpoint}/openai/deployments?api-version={api_version}"
        try:
            r = requests.get(url, headers={"api-key": api_key}, timeout=8)
        except requests.RequestException as e:
            raise ProviderTestError(f"Could not reach Azure: {e}")
        if r.status_code == 401:
            raise ProviderTestError("Azure rejected the api_key (401).")
        if r.status_code == 404:
            raise ProviderTestError(
                "404 from Azure — wrong endpoint, wrong api_version, or the "
                "resource has no deployments yet."
            )
        if r.status_code >= 400:
            raise ProviderTestError(
                f"Azure returned {r.status_code}: {r.text[:200]}"
            )
        # If a model_id was provided, verify it actually exists as a deployment.
        if model_id:
            deployments = (r.json() or {}).get("data") or []
            names = {d.get("id") for d in deployments if d.get("id")}
            if model_id not in names:
                raise ProviderTestError(
                    f"Deployment {model_id!r} not found in this resource. "
                    f"Available: {sorted(names)}"
                )

    def list_models(self, credentials: dict[str, str]) -> list[str]:
        try:
            api_key, endpoint, api_version = self._check_creds(credentials)
        except ProviderTestError:
            return []
        url = f"{endpoint}/openai/deployments?api-version={api_version}"
        try:
            r = requests.get(url, headers={"api-key": api_key}, timeout=8)
            r.raise_for_status()
        except Exception:
            return []
        return sorted(
            d["id"]
            for d in (r.json().get("data") or [])
            if d.get("id") and d.get("status") in {None, "succeeded"}
        )
