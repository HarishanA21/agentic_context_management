"""AWS Bedrock adapter.

Uses `ChatBedrockConverse` from `langchain-aws` — Anthropic's Converse API
gives a uniform shape across all Bedrock-hosted models (Claude, Llama,
Mistral, etc.). Authentication is per-row IAM credentials (access key +
secret) rather than the boto3 default-chain so users can configure
multiple AWS accounts.
"""

from __future__ import annotations

import os
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from providers.base import CredentialField, LLMProvider, ProviderTestError


_DEFAULT_MAX_TOKENS = int(os.getenv("CHAT_MAX_TOKENS", "4096"))
_DEFAULT_TEMPERATURE = float(os.getenv("CHAT_TEMPERATURE", "0.3"))

# Regions Bedrock supports as of late 2025 / early 2026. Sourced from
# AWS docs; offered as a dropdown so users can't typo into 404s.
_BEDROCK_REGIONS = [
    "us-east-1",
    "us-east-2",
    "us-west-2",
    "ca-central-1",
    "sa-east-1",
    "eu-central-1",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "eu-north-1",
    "ap-south-1",
    "ap-northeast-1",
    "ap-northeast-2",
    "ap-southeast-1",
    "ap-southeast-2",
]


class BedrockProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(
            slug="bedrock",
            label="AWS Bedrock",
            description=(
                "Anthropic, Meta, Mistral, AI21 etc. via AWS. Requires an "
                "AWS account with Bedrock model access enabled in the chosen "
                "region (request access in the Bedrock console first)."
            ),
            supports_model_listing=True,
            suggested_models=[
                "anthropic.claude-3-5-sonnet-20241022-v2:0",
                "anthropic.claude-3-5-haiku-20241022-v1:0",
                "anthropic.claude-3-opus-20240229-v1:0",
                "meta.llama3-70b-instruct-v1:0",
                "meta.llama3-1-70b-instruct-v1:0",
                "mistral.mistral-large-2407-v1:0",
            ],
            credential_fields=[
                CredentialField(
                    name="aws_access_key_id",
                    label="Access key ID",
                    placeholder="AKIA...",
                ),
                CredentialField(
                    name="aws_secret_access_key",
                    label="Secret access key",
                    placeholder="(40-char string)",
                ),
                CredentialField(
                    name="aws_region",
                    label="Region",
                    secret=False,
                    placeholder="us-east-1",
                    options=_BEDROCK_REGIONS,
                    help_text=(
                        "Pick a region where you've already requested model "
                        "access via the Bedrock console."
                    ),
                ),
            ],
        )

    def _check_creds(self, credentials: dict[str, str]) -> tuple[str, str, str]:
        ak = credentials.get("aws_access_key_id", "").strip()
        sk = credentials.get("aws_secret_access_key", "").strip()
        rg = credentials.get("aws_region", "").strip()
        if not ak or not sk or not rg:
            raise ProviderTestError(
                "Bedrock requires aws_access_key_id, aws_secret_access_key, "
                "and aws_region."
            )
        return ak, sk, rg

    def build_chat_model(
        self,
        model_id: str,
        credentials: dict[str, str],
        **runtime: Any,
    ) -> BaseChatModel:
        ak, sk, rg = self._check_creds(credentials)
        # Lazy import: langchain-aws pulls in boto3-bedrock at module load.
        try:
            from langchain_aws import ChatBedrockConverse
        except ImportError:
            raise ProviderTestError(
                "langchain-aws is not installed. Run "
                "`pip install -r backend/requirements.txt` and restart."
            )
        return ChatBedrockConverse(
            model=model_id,
            region_name=rg,
            aws_access_key_id=ak,
            aws_secret_access_key=sk,
            max_tokens=runtime.get("max_tokens", _DEFAULT_MAX_TOKENS),
            temperature=runtime.get("temperature", _DEFAULT_TEMPERATURE),
        )

    def test_credentials(
        self, _model_id: str, credentials: dict[str, str]
    ) -> None:
        # `list_foundation_models` doesn't filter by model and is the
        # cheapest authenticated Bedrock call — model_id is unused here.
        ak, sk, rg = self._check_creds(credentials)
        # `boto3` is already a hard dep (storage.py), so we can import it
        # eagerly here.
        try:
            import boto3
            from botocore.exceptions import (
                BotoCoreError,
                ClientError,
                NoCredentialsError,
            )
        except ImportError:
            raise ProviderTestError("boto3 is not installed.")

        try:
            client = boto3.client(
                "bedrock",
                region_name=rg,
                aws_access_key_id=ak,
                aws_secret_access_key=sk,
            )
            client.list_foundation_models()
        except NoCredentialsError:
            raise ProviderTestError("AWS rejected the credentials.")
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in {"InvalidSignatureException", "UnrecognizedClientException"}:
                raise ProviderTestError(
                    "AWS rejected the access key / secret pair."
                )
            if code == "AccessDeniedException":
                raise ProviderTestError(
                    "This IAM user / role doesn't have bedrock:ListFoundationModels. "
                    "Attach the AmazonBedrockFullAccess policy (or equivalent)."
                )
            raise ProviderTestError(f"AWS error: {code} — {e}")
        except BotoCoreError as e:
            raise ProviderTestError(f"AWS connection error: {e}")

    def list_models(self, credentials: dict[str, str]) -> list[str]:
        try:
            ak, sk, rg = self._check_creds(credentials)
        except ProviderTestError:
            return []
        try:
            import boto3
        except ImportError:
            return []
        try:
            client = boto3.client(
                "bedrock",
                region_name=rg,
                aws_access_key_id=ak,
                aws_secret_access_key=sk,
            )
            resp = client.list_foundation_models()
        except Exception:
            return []
        ids = []
        for m in resp.get("modelSummaries", []) or []:
            mid = m.get("modelId")
            modalities = set(m.get("inputModalities") or [])
            # We only want chat-capable text models.
            if mid and "TEXT" in modalities:
                ids.append(mid)
        return sorted(set(ids))
