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
                # aiter_bytes() yields content-decoded bytes (httpx undoes the
                # upstream's gzip/br). aiter_raw() would forward still-compressed
                # bytes, which the IDE — told it's plain text/event-stream —
                # can't parse, so it silently retries. See the SSE passthrough.
                async for chunk in resp.aiter_bytes():
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
                if resp.status_code >= 400:
                    # Surface the upstream error inside the SSE stream instead of
                    # forwarding a JSON error body as text/event-stream (which the
                    # IDE can't parse, so it silently retries). Mirrors the
                    # Anthropic stream path.
                    import json as _json

                    raw = await resp.aread()
                    try:
                        err = _json.loads(raw.decode())
                    except Exception:
                        err = {"error": {"message": raw.decode(errors="replace")[:1000]}}
                    yield (f"data: {_json.dumps(err)}\n\n").encode()
                    yield b"data: [DONE]\n\n"
                    return
                # aiter_bytes() yields content-decoded bytes (httpx undoes the
                # upstream's gzip/br). aiter_raw() would forward still-compressed
                # bytes, which the IDE — told it's plain text/event-stream —
                # can't parse, so it silently retries. See the SSE passthrough.
                async for chunk in resp.aiter_bytes():
                    yield chunk

    async def chat(self, body: Dict[str, Any]) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            resp = await client.post(self.url, headers=self.headers, json=body)
            resp.raise_for_status()
            return resp.json()


class AnthropicUpstream:
    """Forward to a real Anthropic Messages API (``/messages``). Auth + version
    go in headers (``x-api-key`` / ``anthropic-version``), not a bearer token.

    Two auth modes:
      * **api-key** (default) — inject our own ``x-api-key`` (bills API credits).
      * **passthrough** — forward the client's own ``Authorization: Bearer``
        header instead. Claude Code on a Claude *subscription* authenticates with
        an OAuth token; forwarding it untouched lets the request bill the
        subscription, so we can monitor it without an API key. The caller passes
        ``auth_header`` (the verbatim ``Authorization`` value) plus
        ``passthrough_headers`` (the client's identity headers — user-agent,
        ``x-app``, ``x-stainless-*``, ``anthropic-version``) the OAuth path checks.
    """

    def __init__(self, base_url: str, api_key: str | None, version: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.version = version

    def _headers(
        self,
        beta: str | None = None,
        *,
        auth_header: str | None = None,
        passthrough_headers: Dict[str, str] | None = None,
    ) -> Dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "anthropic-version": self.version,
        }
        # Client identity headers first, so a forwarded anthropic-version wins
        # over our default while we still set the load-bearing ones below.
        if passthrough_headers:
            h.update(passthrough_headers)
        if auth_header:
            # Subscription / OAuth passthrough: forward the caller's own
            # credential. Do NOT also send x-api-key — mixing the two 400s.
            h["Authorization"] = auth_header
        elif self.api_key:
            h["x-api-key"] = self.api_key
        # Forward the client's anthropic-beta header — Claude Code gates
        # features (interleaved thinking, fine-grained tool streaming, 1M
        # context, …) behind it; dropping it turns the body into a 400.
        if beta:
            h["anthropic-beta"] = beta
        return h

    def _url(self, beta_query: bool) -> str:
        return f"{self.base_url}/messages" + ("?beta=true" if beta_query else "")

    async def messages_stream(
        self,
        body: Dict[str, Any],
        *,
        beta: str | None = None,
        beta_query: bool = False,
        auth_header: str | None = None,
        passthrough_headers: Dict[str, str] | None = None,
    ) -> AsyncIterator[bytes]:
        headers = self._headers(
            beta, auth_header=auth_header, passthrough_headers=passthrough_headers
        )
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST", self._url(beta_query), headers=headers, json=body
            ) as resp:
                if resp.status_code >= 400:
                    # Surface the upstream error inside the SSE stream so the
                    # IDE shows the real reason instead of choking on a JSON
                    # error body it expected to be event-stream.
                    import json as _json

                    raw = await resp.aread()
                    try:
                        err = _json.loads(raw.decode())
                    except Exception:
                        err = {"type": "error", "error": {
                            "type": "upstream_error",
                            "message": raw.decode(errors="replace")[:1000]}}
                    yield (f"event: error\ndata: {_json.dumps(err)}\n\n").encode()
                    return
                # aiter_bytes() yields content-decoded bytes (httpx undoes the
                # upstream's gzip/br). aiter_raw() would forward still-compressed
                # bytes, which the IDE — told it's plain text/event-stream —
                # can't parse, so it silently retries.
                async for chunk in resp.aiter_bytes():
                    yield chunk

    async def messages(
        self,
        body: Dict[str, Any],
        *,
        beta: str | None = None,
        beta_query: bool = False,
        auth_header: str | None = None,
        passthrough_headers: Dict[str, str] | None = None,
    ) -> Dict[str, Any]:
        headers = self._headers(
            beta, auth_header=auth_header, passthrough_headers=passthrough_headers
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            resp = await client.post(
                self._url(beta_query), headers=headers, json=body
            )
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
