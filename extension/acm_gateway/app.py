"""FastAPI app for the gateway.

Endpoints:
  * ``POST /v1/chat/completions`` — OpenAI-compatible. The IDE points its
    "OpenAI base URL" at ``http://127.0.0.1:8807/v1`` and the request flows
    through the technique pipeline before being forwarded upstream.
  * ``GET  /status`` — what the IDE settings panels poll: active profile, which
    techniques are on, and the last batch of fired events.
  * ``GET  /v1/models`` — pass-through so pickers that list models still work.

The Anthropic-compatible ``/v1/messages`` surface is stubbed (see TODO) — the
shape differs enough to warrant its own translator; the OpenAI path is the
common case for Cursor/VSCode custom endpoints.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from langchain_core.messages import HumanMessage, SystemMessage

from acm_engine import (
    BUILTIN_PRESETS,
    DEFAULT_SUMMARY_SYSTEM,
    PRESET_SUMMARY,
    parse_profile,
)
from acm_mcp.memory_store import MemoryStore

from .config import (
    Settings,
    active_config_path,
    load_profile,
    load_visual_cfg,
    user_config_path,
)
from .droplist import DropStore, conversation_key
from .pipeline import run_pipeline
from .providers_store import ProviderStore
from .translate import lc_to_openai, openai_to_lc
from .translate_anthropic import anthropic_to_lc, lc_to_anthropic
from .upstream import (
    AnthropicSummariser,
    AnthropicUpstream,
    GenericUpstream,
    SummariserClient,
    Upstream,
)

app = FastAPI(title="acm-gateway", version="0.1.0")

_SETTINGS = Settings.from_env()
_LAST_EVENTS: List[Dict[str, Any]] = []
_MEMORY = MemoryStore()
_DROP = DropStore()
_PROVIDERS = ProviderStore()


def _env_openai() -> tuple:
    return (_SETTINGS.upstream_base_url, _SETTINGS.upstream_api_key)


def _env_anthropic() -> tuple:
    return (_SETTINGS.anthropic_base_url, _SETTINGS.anthropic_api_key)


def _resolve(model: str, request: Request):
    """Pick the provider/target for this request (header > model-prefix > default
    > env fallback)."""
    return _PROVIDERS.resolve(
        model or "",
        provider_hint=request.headers.get("x-acm-provider"),
        env_openai=_env_openai(),
        env_anthropic=_env_anthropic(),
    )


def _conv_key(request: Request, messages) -> str:
    """Conversation id from an explicit header, else derived from the prefix."""
    explicit = request.headers.get("x-acm-conversation")
    return conversation_key(messages, explicit)


def _apply_droplist(conv: str, messages):
    """Record the current view for the UI, then strip dropped messages."""
    _DROP.record_seen(conv, messages)
    filtered, removed = _DROP.apply(conv, messages)
    if removed:
        _record([{"type": "manual_removal", "removed": removed, "conversation": conv}])
    return filtered


def _upstream() -> Upstream:
    return Upstream(_SETTINGS.upstream_base_url, _SETTINGS.upstream_api_key)


def _anthropic_upstream() -> AnthropicUpstream:
    return AnthropicUpstream(
        _SETTINGS.anthropic_base_url,
        _SETTINGS.anthropic_api_key,
        _SETTINGS.anthropic_version,
    )


def _record(events: List[Dict[str, Any]]) -> None:
    """Stamp + retain fired events for /status (and log them)."""
    if not events:
        return
    global _LAST_EVENTS
    stamp = time.time()
    _LAST_EVENTS += [{"ts": stamp, **e} for e in events]
    _LAST_EVENTS = _LAST_EVENTS[-100:]
    if _SETTINGS.log_events:
        print(f"[acm-gateway] fired: {json.dumps(events)[:500]}", flush=True)


@app.get("/status")
def status() -> Dict[str, Any]:
    profile = load_profile(active_config_path())
    cm = profile.context_management
    enabled = {
        "tool_result_trimming": cm.tool_result_trimming.enabled,
        "summarization": cm.summarization.enabled,
        "sliding_window": cm.sliding_window.enabled,
        "image_recall": cm.image_recall.mode,
        "memory": cm.memory.enabled,
        "subagent": cm.subagent.enabled,
        "jit_tools": cm.jit_tools.enabled,
        "visual_method": bool(load_visual_cfg(active_config_path()).get("enabled")),
    }
    prov = _PROVIDERS.list(mask=True)
    return {
        "ok": True,
        "upstream": _SETTINGS.upstream_base_url,
        "config_path": str(active_config_path()),
        "tool_surface": profile.tool_surface,
        "techniques": enabled,
        "providers": {
            "default": prov["default"],
            "configured": sorted(prov["providers"].keys()),
        },
        "last_events": _LAST_EVENTS[-20:],
    }


def _pick_summariser(model: str | None):
    """Return a ``.invoke(messages)`` summariser bound to whichever upstream has
    a key. Prefers OpenAI-style (covers OpenRouter); falls back to Anthropic."""
    if _SETTINGS.upstream_api_key:
        return SummariserClient(
            _SETTINGS.upstream_base_url,
            _SETTINGS.upstream_api_key,
            model or "openai/gpt-4o-mini",
        )
    if _SETTINGS.anthropic_api_key:
        return AnthropicSummariser(
            _SETTINGS.anthropic_base_url,
            _SETTINGS.anthropic_api_key,
            _SETTINGS.anthropic_version,
            model or "claude-haiku-4-5-20251001",
        )
    return None


@app.post("/compact")
async def compact(request: Request) -> Any:
    """Compact an arbitrary transcript into a short carry-over note using the
    real summariser. Used by the Cursor `stop` hook to turn a captured session
    trail into a single summary. Body: ``{text, instructions?, model?}``."""
    body: Dict[str, Any] = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "missing 'text'"}, status_code=400)

    summariser = _pick_summariser(body.get("model"))
    if summariser is None:
        return JSONResponse(
            {"error": "no upstream API key configured for compaction"},
            status_code=503,
        )

    system_text = DEFAULT_SUMMARY_SYSTEM
    if body.get("instructions"):
        system_text += "\n\nExtra instructions:\n" + str(body["instructions"])
    try:
        resp = summariser.invoke(
            [
                SystemMessage(content=system_text),
                HumanMessage(content=f"Summarise the following transcript:\n\n{text}"),
            ]
        )
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)

    summary = resp.content if isinstance(resp.content, str) else str(resp.content)
    return JSONResponse({"summary": summary.strip()})


_SUBAGENT_SYSTEM = (
    "You are an isolated sub-agent. You receive ONE task and must return ONE "
    "short answer. Do the reasoning internally; the caller will only see your "
    "final answer, so none of your intermediate work should appear. Wrap the "
    "answer in <summary>...</summary> and keep it under 500 tokens."
)
_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.S | re.I)


@app.post("/subagent")
async def subagent(request: Request) -> Any:
    """Run an isolated sub-task and return only its conclusion — the sub-agent's
    reasoning never enters the caller's context. Body: ``{task, context?,
    model?}``. (IDE port: the sub-agent reasons over the provided context; it
    does not get the host's tools — that's the host agent's job.)"""
    body: Dict[str, Any] = await request.json()
    task = (body.get("task") or "").strip()
    if not task:
        return JSONResponse({"error": "missing 'task'"}, status_code=400)

    worker = _pick_summariser(body.get("model"))
    if worker is None:
        return JSONResponse(
            {"error": "no upstream API key configured for the sub-agent"},
            status_code=503,
        )
    context = (body.get("context") or "").strip()
    user = f"Task: {task}"
    if context:
        user += f"\n\nContext you may use:\n{context}"
    try:
        resp = worker.invoke(
            [SystemMessage(content=_SUBAGENT_SYSTEM), HumanMessage(content=user)]
        )
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)

    text = resp.content if isinstance(resp.content, str) else str(resp.content)
    m = _SUMMARY_RE.search(text or "")
    summary = (m.group(1).strip() if m else (text or "").strip()) or "(sub-agent produced no output)"
    return JSONResponse({"summary": summary})


# ─── control plane (used by the VSCode extension over HTTP) ──────────────


@app.get("/profile")
def get_profile() -> Dict[str, Any]:
    """Active profile + the gateway-only visual_method block + built-in presets,
    for the settings UI."""
    profile = load_profile(active_config_path())
    return {
        "active": profile.model_dump(),
        "visual_method": load_visual_cfg(active_config_path()),
        "config_path": str(active_config_path()),
        "presets": [
            {
                "name": p["name"],
                "summary": PRESET_SUMMARY.get(p["name"]),
                "body": p["body"],
            }
            for p in BUILTIN_PRESETS
        ],
    }


@app.post("/profile")
async def set_profile(request: Request) -> Any:
    """Set the active profile. Body is ``{"name": "<preset>"}`` or
    ``{"body": {<full Profile dict>}}``, plus an optional ``"visual_method"``
    block (the gateway-only axis). Always writes the user config copy and
    preserves any existing ``visual_method`` unless a new one is supplied."""
    body: Dict[str, Any] = await request.json()
    name = body.get("name")
    if name:
        preset = next((p for p in BUILTIN_PRESETS if p["name"] == name), None)
        if preset is None:
            return JSONResponse(
                {"error": f"unknown preset '{name}'"}, status_code=400
            )
        new_body = dict(preset["body"])
    elif body.get("body"):
        try:
            new_body = parse_profile(body["body"]).model_dump()
        except Exception as e:
            return JSONResponse({"error": f"invalid profile: {e}"}, status_code=400)
    else:
        return JSONResponse(
            {"error": "provide 'name' or 'body'"}, status_code=400
        )
    # Preserve / update the visual_method block (it isn't part of the Profile).
    visual = body.get("visual_method")
    if visual is None:
        visual = load_visual_cfg(active_config_path())
    new_body["visual_method"] = visual

    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(new_body, indent=2))
    return JSONResponse(
        {"ok": True, "active": new_body, "visual_method": visual, "config_path": str(path)}
    )


@app.post("/memory/remember")
async def memory_remember(request: Request) -> Any:
    body: Dict[str, Any] = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "missing 'text'"}, status_code=400)
    n = _MEMORY.remember(text, scope=body.get("scope", "user"))
    return JSONResponse({"ok": True, "count": n})


@app.get("/memory/recall")
def memory_recall(query: str = "", scope: str = "user", limit: int = 10) -> Dict[str, Any]:
    return {"items": _MEMORY.recall(query, scope=scope, limit=limit)}


@app.post("/memory/clear")
async def memory_clear(request: Request) -> Any:
    body: Dict[str, Any] = await request.json()
    _MEMORY.clear(scope=body.get("scope", "user"))
    return JSONResponse({"ok": True})


# ─── manual message removal (drop-list) ──────────────────────────────────


@app.get("/conversations")
def list_conversations() -> Dict[str, Any]:
    """Conversations the gateway has seen this run, newest first (for the UI)."""
    return {"conversations": _DROP.conversations()}


@app.get("/messages")
def list_messages(conv: str = "") -> Dict[str, Any]:
    """Last-seen messages for a conversation: fingerprint, role, preview, and
    whether each is currently dropped. ``conv`` defaults to the latest."""
    key = conv or _DROP.latest_conversation() or ""
    return {"conversation": key, "messages": _DROP.seen(key)}


@app.post("/messages/drop")
async def drop_message(request: Request) -> Any:
    """Tombstone a message so the model never sees it again. Body:
    ``{conv?, fp}`` (conv defaults to the latest conversation)."""
    body: Dict[str, Any] = await request.json()
    fp = body.get("fp")
    if not fp:
        return JSONResponse({"error": "missing 'fp'"}, status_code=400)
    conv = body.get("conv") or _DROP.latest_conversation()
    if not conv:
        return JSONResponse({"error": "no conversation known yet"}, status_code=400)
    _DROP.drop(conv, fp)
    return JSONResponse({"ok": True, "conversation": conv, "dropped": _DROP.dropped(conv)})


@app.post("/messages/restore")
async def restore_message(request: Request) -> Any:
    """Un-tombstone a message. Body: ``{conv?, fp}``."""
    body: Dict[str, Any] = await request.json()
    fp = body.get("fp")
    if not fp:
        return JSONResponse({"error": "missing 'fp'"}, status_code=400)
    conv = body.get("conv") or _DROP.latest_conversation()
    if not conv:
        return JSONResponse({"error": "no conversation known yet"}, status_code=400)
    ok = _DROP.restore(conv, fp)
    return JSONResponse({"ok": ok, "conversation": conv, "dropped": _DROP.dropped(conv)})


# ─── multi-provider management ───────────────────────────────────────────


@app.get("/providers")
def list_providers() -> Dict[str, Any]:
    """Configured providers (api keys masked) + the default. Falls back to the
    env upstream when none are configured."""
    listing = _PROVIDERS.list(mask=True)
    listing["env_fallback"] = {
        "openai_base": _SETTINGS.upstream_base_url,
        "anthropic_base": _SETTINGS.anthropic_base_url,
        "openai_key_set": bool(_SETTINGS.upstream_api_key),
        "anthropic_key_set": bool(_SETTINGS.anthropic_api_key),
    }
    listing["supported_types"] = sorted(
        {"openai", "openrouter", "google", "azure", "anthropic", "custom"}
    )
    return listing


@app.post("/providers")
async def upsert_provider(request: Request) -> Any:
    """Add or update a provider. Body: ``{slug, type, api_key?, base_url?,
    azure_endpoint?, api_version?, organization?, default?}``."""
    body: Dict[str, Any] = await request.json()
    slug = (body.get("slug") or "").strip()
    if not slug:
        return JSONResponse({"error": "missing 'slug'"}, status_code=400)
    try:
        _PROVIDERS.upsert(slug, body)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, **_PROVIDERS.list(mask=True)})


@app.delete("/providers/{slug}")
def delete_provider(slug: str) -> Any:
    ok = _PROVIDERS.delete(slug)
    return JSONResponse({"ok": ok, **_PROVIDERS.list(mask=True)})


@app.post("/providers/{slug}/default")
def set_default_provider(slug: str) -> Any:
    ok = _PROVIDERS.set_default(slug)
    return JSONResponse({"ok": ok, **_PROVIDERS.list(mask=True)})


@app.get("/v1/models")
async def models() -> Any:
    """Pass-through to the upstream model list so IDE pickers keep working."""
    import httpx

    headers = {}
    if _SETTINGS.upstream_api_key:
        headers["Authorization"] = f"Bearer {_SETTINGS.upstream_api_key}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{_SETTINGS.upstream_base_url}/models", headers=headers
        )
        return JSONResponse(resp.json(), status_code=resp.status_code)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    body: Dict[str, Any] = await request.json()
    profile = load_profile(active_config_path())

    incoming = body.get("messages", []) or []
    lc_messages = openai_to_lc(incoming)

    # Manual removal: strip any tombstoned messages before anything else.
    conv = _conv_key(request, lc_messages)
    lc_messages = _apply_droplist(conv, lc_messages)

    summariser = None
    if profile.context_management.summarization.enabled:
        # Reuse the request's own model for compaction unless the profile names
        # a cheaper one.
        slug = profile.context_management.summarization.summariser_model or body.get(
            "model"
        )
        if slug:
            summariser = SummariserClient(
                _SETTINGS.upstream_base_url, _SETTINGS.upstream_api_key, slug
            )

    new_messages, events = run_pipeline(
        lc_messages,
        profile,
        summariser=summariser,
        visual_cfg=load_visual_cfg(active_config_path()),
    )
    _record(events)

    # Rebuild the forwarded body with the rewritten messages.
    body = dict(body)
    body["messages"] = lc_to_openai(new_messages)

    # Route to the selected provider (header > model-prefix > default > env).
    target = _resolve(body.get("model", ""), request)
    if target.kind == "anthropic":
        return JSONResponse(
            {
                "error": (
                    f"provider '{target.slug}' is Anthropic-native — call the "
                    "/v1/messages endpoint for it, not /v1/chat/completions"
                )
            },
            status_code=400,
        )
    body["model"] = target.model
    up = GenericUpstream(target.url, target.headers or {})
    if body.get("stream"):
        return StreamingResponse(
            up.chat_stream(body), media_type="text/event-stream"
        )
    data = await up.chat(body)
    return JSONResponse(data)


@app.post("/v1/messages")
async def anthropic_messages(request: Request) -> Any:
    """Anthropic-native surface (Claude Code). Same pipeline as the OpenAI path,
    using the Messages-API translator + an Anthropic upstream."""
    body: Dict[str, Any] = await request.json()
    profile = load_profile(active_config_path())

    lc_messages = anthropic_to_lc(body.get("system"), body.get("messages", []) or [])

    # Manual removal: strip tombstoned messages before the pipeline.
    conv = _conv_key(request, lc_messages)
    lc_messages = _apply_droplist(conv, lc_messages)

    summariser = None
    if profile.context_management.summarization.enabled:
        slug = profile.context_management.summarization.summariser_model or body.get(
            "model"
        )
        if slug:
            summariser = AnthropicSummariser(
                _SETTINGS.anthropic_base_url,
                _SETTINGS.anthropic_api_key,
                _SETTINGS.anthropic_version,
                slug,
            )

    new_messages, events = run_pipeline(
        lc_messages,
        profile,
        summariser=summariser,
        visual_cfg=load_visual_cfg(active_config_path()),
    )
    _record(events)

    new_system, new_msgs = lc_to_anthropic(new_messages)
    body = dict(body)
    body["messages"] = new_msgs
    if new_system is not None:
        body["system"] = new_system
    elif "system" in body:
        del body["system"]

    # Route to a configured Anthropic provider if one is selected, else env.
    target = _resolve(body.get("model", ""), request)
    if target.kind == "anthropic":
        up = AnthropicUpstream(
            target.base_url, target.api_key, _SETTINGS.anthropic_version
        )
        body["model"] = target.model
    else:
        up = _anthropic_upstream()
    if body.get("stream"):
        return StreamingResponse(
            up.messages_stream(body), media_type="text/event-stream"
        )
    data = await up.messages(body)
    return JSONResponse(data)
