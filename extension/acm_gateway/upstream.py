"""Forward a (rewritten) request to the real OpenAI-compatible provider.

Kept deliberately small: the gateway's value is the *rewrite*, not re-inventing
an SDK. We pass the body through with ``httpx`` and stream the response back
byte-for-byte so the IDE sees exactly what the upstream produced.

Also exposes :class:`SummariserClient`, a minimal ``.invoke(messages)`` shim so
the summarization technique (which expects a LangChain-style chat model) can run
against the same upstream.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List

import httpx
from langchain_core.messages import BaseMessage

from .translate import lc_to_openai


class Upstream:
    def __init__(self, base_url: str, api_key: str | None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _headers(self, extra: Dict[str, str] | None = None) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if extra:
            h.update(extra)
        return h

    async def chat_stream(self, body: Dict[str, Any]) -> AsyncIterator[bytes]:
        """Stream ``/chat/completions`` (SSE) back to the caller."""
        url = f"{self.base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST", url, headers=self._headers(), json=body
            ) as resp:
                async for chunk in resp.aiter_raw():
                    yield chunk

    async def chat(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Non-streaming ``/chat/completions`` -> parsed JSON."""
        url = f"{self.base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            resp = await client.post(url, headers=self._headers(), json=body)
            resp.raise_for_status()
            return resp.json()


class GenericUpstream:
    """Forward to a fully-resolved OpenAI-compatible chat endpoint — the URL and
    auth headers are computed by the provider router, so this works for OpenAI,
    OpenRouter, Google (OpenAI-compat), and Azure alike."""

    def __init__(self, url: str, headers: Dict[str, str]) -> None:
        self.url = url
        self.headers = {"Content-Type": "application/json", **(headers or {})}

    async def chat_stream(self, body: Dict[str, Any]) -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST", self.url, headers=self.headers, json=body
            ) as resp:
                async for chunk in resp.aiter_raw():
                    yield chunk

    async def chat(self, body: Dict[str, Any]) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            resp = await client.post(self.url, headers=self.headers, json=body)
            resp.raise_for_status()
            return resp.json()


class AnthropicUpstream:
    """Forward to a real Anthropic Messages API (``/messages``). Auth + version
    go in headers (``x-api-key`` / ``anthropic-version``), not a bearer token."""

    def __init__(self, base_url: str, api_key: str | None, version: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.version = version

    def _headers(self) -> Dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "anthropic-version": self.version,
        }
        if self.api_key:
            h["x-api-key"] = self.api_key
        return h

    async def messages_stream(self, body: Dict[str, Any]) -> AsyncIterator[bytes]:
        url = f"{self.base_url}/messages"
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST", url, headers=self._headers(), json=body
            ) as resp:
                async for chunk in resp.aiter_raw():
                    yield chunk

    async def messages(self, body: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/messages"
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            resp = await client.post(url, headers=self._headers(), json=body)
            resp.raise_for_status()
            return resp.json()


class AnthropicSummariser:
    """``.invoke(messages)`` shim over the Anthropic Messages API, so the
    summarization technique can run on the Claude Code path. The engine only
    ever passes one SystemMessage + one HumanMessage, so we map those directly."""

    def __init__(self, base_url: str, api_key: str | None, version: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.version = version
        self.model = model

    def invoke(self, messages: List[BaseMessage]) -> Any:
        system = ""
        user = ""
        for m in messages:
            content = getattr(m, "content", "")
            text = content if isinstance(content, str) else str(content)
            if m.__class__.__name__ == "SystemMessage":
                system = text
            else:
                user = text
        body = {
            "model": self.model,
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {"Content-Type": "application/json", "anthropic-version": self.version}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        with httpx.Client(timeout=httpx.Timeout(120.0)) as client:
            resp = client.post(f"{self.base_url}/messages", headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        # Anthropic returns content as a list of blocks; join the text ones.
        parts = [
            b.get("text", "")
            for b in data.get("content", [])
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return _Resp("\n".join(p for p in parts if p))


class SummariserClient:
    """Adapts the upstream to the ``.invoke(list[BaseMessage]) -> resp`` shape
    that ``summarise_old_messages`` expects. Synchronous on purpose — the engine
    calls it synchronously inside the pipeline."""

    def __init__(self, base_url: str, api_key: str | None, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def invoke(self, messages: List[BaseMessage]) -> Any:
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": self.model,
            "messages": lc_to_openai(messages),
            "temperature": 0,
        }
        with httpx.Client(timeout=httpx.Timeout(120.0)) as client:
            resp = client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        text = (
            data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        )
        return _Resp(text)


class _Resp:
    """Tiny stand-in for a LangChain AIMessage (only ``.content`` is read)."""

    def __init__(self, content: str) -> None:
        self.content = content
