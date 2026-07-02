"""FastAPI app for the gateway — an LLM proxy that rewrites the context window.

The IDE points its model endpoint at this server instead of the real provider.
Each request flows through the technique pipeline (trim / summarise / evict /
sliding-window / cache) before being forwarded upstream, so we edit the context
window *on the wire* — the position the LangGraph middleware occupies in-process.

Endpoints:
  * ``POST /v1/chat/completions`` — OpenAI-compatible. The IDE points its
    "OpenAI base URL" at ``http://127.0.0.1:8807/v1`` (Cursor, Continue, Cline,
    any OpenAI-compatible client). Rewrites, then forwards to the resolved
    OpenAI-style provider.
  * ``POST /v1/messages`` — Anthropic-native. Point Claude Code at it with
    ``ANTHROPIC_BASE_URL=http://127.0.0.1:8807``. Same pipeline, via the
    Messages-API translator + an Anthropic upstream (forwards the
    ``anthropic-beta`` header / ``?beta=true``).
  * ``GET  /v1/models`` — pass-through so model pickers keep working.
  * ``GET  /status`` + the control-plane routes — what the IDE settings panels
    poll: active profile, enabled techniques, recent fired events, memory,
    drop-list, relevance, providers.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from langchain_core.messages import HumanMessage, SystemMessage

from acm_engine import (
    BUILTIN_PRESETS,
    DEFAULT_SUMMARY_SYSTEM,
    EncoderSuggester,
    PRESET_SUMMARY,
    active_task,
    build_audit_rows,
    parse_profile,
    record_audit,
    record_feedback,
    suggest_removals,
)
from acm_mcp.memory_store import MemoryStore

from .config import (
    Settings,
    active_config_path,
    load_profile,
    load_visual_cfg,
    user_config_path,
)
from . import events
from .context_windows import ContextWindowStore, in_project
from .droplist import (
    DropStore,
    _norm_text,
    conversation_key,
    fingerprint,
    project_path,
    session_namespace,
)
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
_WINDOWS = ContextWindowStore()


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


def _conv_key(request: Request, messages, body: Dict[str, Any] | None = None) -> str:
    """Conversation (context-window) id: an explicit header, else the settled
    prefix hash namespaced by the Claude Code session id when present."""
    explicit = request.headers.get("x-acm-conversation")
    return conversation_key(messages, explicit, namespace=session_namespace(body))


def _resolve_profile(conv: str):
    """The technique profile for this chat: an inline per-window body wins, then
    a named preset override, then the global active profile (the default every
    new chat inherits until the user changes it)."""
    win = _WINDOWS.get(conv)
    if win:
        body = win.get("profile_body")
        if body:
            try:
                return parse_profile(body)
            except Exception as e:  # pragma: no cover - defensive
                print(f"[acm-gateway] bad per-chat profile for {conv}: {e}", flush=True)
        name = win.get("profile_name")
        if name:
            preset = next((p for p in BUILTIN_PRESETS if p["name"] == name), None)
            if preset:
                try:
                    return parse_profile(preset["body"])
                except Exception:  # pragma: no cover - defensive
                    pass
    return load_profile(active_config_path())


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


# Claude Code's identity headers — forwarded verbatim in OAuth passthrough so the
# subscription endpoint sees the same request shape it expects. Everything else
# (host, content-length, accept-encoding, the client's auth) is dropped/replaced.
_PASSTHROUGH_EXACT = {
    "user-agent",
    "x-app",
    "anthropic-version",
    "anthropic-dangerous-direct-browser-access",
}
_PASSTHROUGH_PREFIXES = ("x-stainless-",)

# What credential the last Anthropic turn used — surfaced by /status so the UI
# can show "monitoring subscription" vs "api-key".
_LAST_AUTH: Dict[str, Any] = {"mode": "api_key", "subscription": False, "token_tail": None}


def _passthrough_headers(request: Request) -> Dict[str, str]:
    """The client's identity headers to forward in OAuth passthrough mode."""
    out: Dict[str, str] = {}
    for k, v in request.headers.items():
        lk = k.lower()
        if lk in _PASSTHROUGH_EXACT or lk.startswith(_PASSTHROUGH_PREFIXES):
            out[k] = v
    return out


def _anthropic_auth_decision(request: Request, target) -> tuple[bool, str | None]:
    """Decide whether to forward the client's own bearer (a Claude *subscription*
    OAuth session) instead of injecting our x-api-key.

    Only applies to the env upstream — an explicitly-configured Anthropic
    provider always authenticates with its own key. Returns
    ``(use_passthrough, auth_header)``."""
    incoming = request.headers.get("authorization")
    has_bearer = bool(incoming and incoming.lower().startswith("bearer "))
    mode = _SETTINGS.anthropic_auth_mode
    if target.kind == "anthropic":
        return False, None
    if mode == "passthrough" and has_bearer:
        return True, incoming
    if mode == "auto" and has_bearer:
        return True, incoming
    return False, None


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
        "relevance_pruning": getattr(
            getattr(cm, "relevance_pruning", None), "enabled", False
        ),
        "visual_method": bool(load_visual_cfg(active_config_path()).get("enabled")),
    }
    prov = _PROVIDERS.list(mask=True)
    latest = _DROP.latest_conversation() or ""
    rows = _DROP.seen(latest) if latest else []
    live_tok = sum(int(r.get("tokens", 0)) for r in rows if not r.get("dropped"))
    saved_tok = sum(int(r.get("tokens", 0)) for r in rows if r.get("dropped"))
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
        # How the Anthropic surface authenticated upstream on the last turn.
        # `subscription` is true when we forwarded Claude Code's own OAuth bearer
        # (so the turn billed the user's plan, not API credits) — drives the HUD.
        "auth": {"configured_mode": _SETTINGS.anthropic_auth_mode, **_LAST_AUTH},
        # Live context of the latest conversation (each message counted once):
        # `tokens` = what the model still sees, `saved_tokens` = what ACM has
        # removed. Drives the Overview gauge + the status-bar HUD.
        "context": {
            "conversation": latest,
            "tokens": live_tok,
            "saved_tokens": saved_tok,
            "messages": len(rows),
            "dropped": sum(1 for r in rows if r.get("dropped")),
        },
        "last_events": _LAST_EVENTS[-20:],
        "notices": _compute_notices(profile),
    }


def _compute_notices(profile) -> List[Dict[str, str]]:
    """Standing degraded-mode warnings for the Overview panel.

    Unlike pipeline notices (per-turn, transient), these describe configuration
    gaps that persist across turns: no summariser key, or a relevance encoder
    that fell back to the untrained lexical backend."""
    notices: List[Dict[str, str]] = []
    cm = profile.context_management

    if getattr(cm.summarization, "enabled", False) and not (
        _SETTINGS.upstream_api_key or _SETTINGS.anthropic_api_key
    ):
        notices.append(
            {
                "level": "warn",
                "step": "summarization",
                "message": "Summarization is on but no upstream API key is configured — it will be skipped. Add one in Providers.",
            }
        )

    rel = getattr(cm, "relevance_pruning", None)
    if rel is not None and getattr(rel, "enabled", False):
        mode = str(getattr(rel, "mode", "judge"))
        if mode in ("encoder", "ensemble"):
            encoder_path = getattr(rel, "encoder_path", None) or _SETTINGS.encoder_path
            enc = _get_encoder(
                encoder_path,
                float(getattr(rel, "drop_threshold", 0.35) or 0.35),
                float(getattr(rel, "summarize_threshold", 0.6) or 0.6),
            )
            if enc is None:
                notices.append(
                    {
                        "level": "error",
                        "step": "relevance_pruning",
                        "message": "Relevance encoder could not be loaded — relevance pruning is unavailable.",
                    }
                )
                return notices
            try:
                enc._ensure_loaded()
            except Exception:  # pragma: no cover - defensive
                pass
            if getattr(enc, "backend", None) == "lexical":
                notices.append(
                    {
                        "level": "warn",
                        "step": "relevance_pruning",
                        "message": "Relevance is using the untrained lexical fallback — suggestions are heuristic. Train or set an encoder model for better accuracy.",
                    }
                )
    return notices


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


@app.get("/events")
async def events_stream() -> StreamingResponse:
    """Realtime push of context-window changes (Server-Sent Events). The VSCode
    host subscribes here and relays each event to its webviews, so the panels
    update the instant a turn flows through instead of waiting for a poll."""
    return StreamingResponse(
        events.stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


@app.get("/context_window")
def context_window(conv: str = "") -> Dict[str, Any]:
    """The exact payload last forwarded upstream for ``conv`` — the post-pipeline
    wire body (system, messages, tools) the model actually saw. Powers the
    Context Window view. ``conv`` defaults to the latest conversation."""
    key = conv or _DROP.latest_conversation() or ""
    return _DROP.sent(key)


# ─── context windows (one per chat: per-chat profile + lifecycle) ─────────


def _window_view(conv: str, include_profile: bool = False) -> Dict[str, Any]:
    """One chat as the UI sees it: the persistent window row merged with live
    token/title stats and the *effective* technique set (after the per-chat
    override or the global default). ``include_profile`` adds the full effective
    Profile body — for the per-chat technique editor."""
    row = _WINDOWS.get(conv) or {"id": conv}
    seen = _DROP.seen(conv)
    eff = _resolve_profile(conv)
    cm = eff.context_management
    view = {
        **row,
        "id": conv,
        "title": row.get("title") or _DROP.title(conv),
        # Live stats win while the conversation is in memory this run; else the
        # values mirrored into the registry (so the list survives a restart).
        "tokens": _DROP.context_tokens(conv) or row.get("tokens", 0),
        "messages": len(seen) or row.get("messages", 0),
        "dropped": sum(1 for r in seen if r.get("dropped")),
        "profile_source": (
            "body"
            if row.get("profile_body")
            else "preset"
            if row.get("profile_name")
            else "global"
        ),
        "techniques": {
            "tool_result_trimming": cm.tool_result_trimming.enabled,
            "summarization": cm.summarization.enabled,
            "sliding_window": cm.sliding_window.enabled,
            "image_recall": cm.image_recall.mode,
            "memory": cm.memory.enabled,
            "subagent": cm.subagent.enabled,
            "jit_tools": cm.jit_tools.enabled,
            "relevance_pruning": getattr(
                getattr(cm, "relevance_pruning", None), "enabled", False
            ),
        },
    }
    if include_profile:
        view["profile"] = eff.model_dump()
    return view


@app.get("/context_windows")
def list_context_windows(project: str = "") -> Dict[str, Any]:
    """Chats' context windows, pinned-then-newest. With ``project`` set, only
    this project's chats (Claude-Code-style per-project history). Includes live
    conversations seen this run even if not yet registered."""
    ids = {w["id"] for w in _WINDOWS.list(project)}
    ids.update(c["key"] for c in _DROP.conversations())
    windows = [_window_view(c) for c in ids]
    if project:
        windows = [w for w in windows if in_project(w.get("project", ""), project)]
    windows.sort(
        key=lambda w: (bool(w.get("pinned")), w.get("last_seen", 0)), reverse=True
    )
    return {"windows": windows}


@app.get("/context_windows/{conv}")
def get_context_window(conv: str) -> Dict[str, Any]:
    return _window_view(conv, include_profile=True)


@app.post("/context_windows/{conv}/profile")
async def set_context_window_profile(conv: str, request: Request) -> Any:
    """Point this chat at a technique profile. Body: ``{"name": "<preset>"}``,
    ``{"body": {<Profile>}}`` (inline), or ``{"clear": true}`` to revert to the
    global default."""
    body: Dict[str, Any] = await request.json()
    if body.get("clear"):
        _WINDOWS.clear_profile(conv)
    elif body.get("name"):
        name = body["name"]
        if not any(p["name"] == name for p in BUILTIN_PRESETS):
            return JSONResponse({"error": f"unknown preset '{name}'"}, status_code=400)
        _WINDOWS.set_profile(conv, name=name)
    elif body.get("body"):
        try:
            parsed = parse_profile(body["body"]).model_dump()
        except Exception as e:
            return JSONResponse({"error": f"invalid profile: {e}"}, status_code=400)
        _WINDOWS.set_profile(conv, body=parsed)
    else:
        return JSONResponse(
            {"error": "provide 'name', 'body', or 'clear'"}, status_code=400
        )
    _record([{"type": "context_window_profile", "conversation": conv}])
    events.publish({"type": "window", "conv": conv, "action": "profile"})
    return JSONResponse(_window_view(conv, include_profile=True))


@app.delete("/context_windows/{conv}")
def delete_context_window(conv: str) -> Any:
    """Delete a chat's context window and purge all of its managed state
    (drop-list, summaries, snapshots)."""
    existed = _WINDOWS.delete(conv)
    _DROP.forget(conv)
    events.publish({"type": "window", "conv": conv, "action": "delete"})
    return JSONResponse({"ok": True, "deleted": existed, "conversation": conv})


@app.post("/context_windows/reset")
def reset_context_windows() -> Any:
    """Wipe every chat and all captured state — context windows, drop-lists,
    summaries, and snapshots. Does not touch provider config or memory."""
    cleared = _WINDOWS.clear()
    _DROP.clear_all()
    events.publish({"type": "window", "conv": "", "action": "reset"})
    return JSONResponse({"ok": True, "cleared": cleared})


@app.post("/context_windows/{conv}/title")
async def rename_context_window(conv: str, request: Request) -> Any:
    body: Dict[str, Any] = await request.json()
    row = _WINDOWS.rename(conv, (body.get("title") or "").strip())
    events.publish({"type": "window", "conv": conv, "action": "rename"})
    return JSONResponse(row)


@app.post("/context_windows/{conv}/pin")
async def pin_context_window(conv: str, request: Request) -> Any:
    body: Dict[str, Any] = await request.json()
    row = _WINDOWS.set_pinned(conv, bool(body.get("pinned", True)))
    events.publish({"type": "window", "conv": conv, "action": "pin"})
    return JSONResponse(row)


@app.get("/messages/text")
def message_text(conv: str = "", fp: str = "") -> Dict[str, Any]:
    """Full text of one message — the seen rows only carry a 120-char preview,
    so the transcript fetches this on expand-to-read."""
    key = conv or _DROP.latest_conversation() or ""
    if not key or not fp:
        return {"text": "", "error": "missing conv/fp"}
    msg = next((m for m in _DROP.seen_full(key) if fingerprint(m) == fp), None)
    if msg is None:
        return {"text": "", "error": "message not found"}
    return {"text": _norm_text(getattr(msg, "content", ""))}


@app.get("/messages/images")
def message_images(conv: str = "", fp: str = "") -> Dict[str, Any]:
    """Visual-method page images for one tool message, as data URLs the webview
    can preview. If the message is already multimodal we return its images;
    otherwise we rasterise its text on demand (same renderer as the pipeline)."""
    key = conv or _DROP.latest_conversation() or ""
    if not key or not fp:
        return {"images": [], "count": 0, "error": "missing conv/fp"}
    msg = next((m for m in _DROP.seen_full(key) if fingerprint(m) == fp), None)
    if msg is None:
        return {"images": [], "count": 0, "error": "message not found"}

    content = getattr(msg, "content", "")
    images: List[str] = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "image_url":
                url = (b.get("image_url") or {}).get("url", "")
                if url:
                    images.append(url)
        if images:
            return {"images": images, "count": len(images)}
        content = _norm_text(content)
    if not isinstance(content, str) or not content.strip():
        return {"images": [], "count": 0}
    try:
        import base64 as _b64

        from visual_tool.rasterizer import render_2col_pages

        for png in render_2col_pages(content):
            images.append(
                "data:image/png;base64," + _b64.b64encode(png).decode("ascii")
            )
    except Exception as e:  # pragma: no cover - defensive
        return {"images": [], "count": 0, "error": f"{type(e).__name__}: {e}"}
    return {"images": images, "count": len(images)}


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


@app.post("/messages/drop_many")
async def drop_many(request: Request) -> Any:
    """Tombstone several messages at once — used when the user accepts a
    relevance suggestion (one episode = many messages). Body: ``{conv?, fps}``."""
    body: Dict[str, Any] = await request.json()
    fps = body.get("fps") or []
    if not isinstance(fps, list) or not fps:
        return JSONResponse({"error": "missing 'fps' (non-empty list)"}, status_code=400)
    conv = body.get("conv") or _DROP.latest_conversation()
    if not conv:
        return JSONResponse({"error": "no conversation known yet"}, status_code=400)
    for fp in fps:
        _DROP.drop(conv, str(fp))
    return JSONResponse(
        {"ok": True, "conversation": conv, "dropped": _DROP.dropped(conv)}
    )


# ─── relevance pruning (task-aware removal suggestions) ──────────────────


# Cache one encoder per (path, thresholds) so the model loads at most once.
_ENCODER_CACHE: Dict[tuple, Any] = {}


def _get_encoder(path: str, drop_t: float, summ_t: float):
    """Build (and cache) the local relevance encoder. Never raises — on any
    construction error returns ``None`` so ensemble mode degrades to judge-only
    and encoder-only mode reports a clean error."""
    cache_key = (path or "", round(drop_t, 3), round(summ_t, 3))
    if cache_key in _ENCODER_CACHE:
        return _ENCODER_CACHE[cache_key]
    try:
        enc = EncoderSuggester(
            path or None, drop_threshold=drop_t, summarize_threshold=summ_t
        )
    except Exception as e:  # pragma: no cover - defensive
        print(f"[acm-gateway] encoder init failed: {e!r}", flush=True)
        enc = None
    _ENCODER_CACHE[cache_key] = enc
    return enc


@app.get("/relevance/suggest")
def relevance_suggest(conv: str = "") -> Dict[str, Any]:
    """Audit the last-seen conversation and return per-episode KEEP/SUMMARIZE/
    DROP suggestions. Suggest-only: nothing is removed here. Each suggestion
    carries ``member_fps`` so the UI can drop the whole episode in one call.

    ``mode`` (from the profile) selects the engine(s): ``judge`` (LLM only),
    ``encoder`` (local model only), or ``ensemble`` (both, reconciled by
    ``arbitration``)."""
    key = conv or _DROP.latest_conversation() or ""
    profile = _resolve_profile(key)
    cfg = getattr(profile.context_management, "relevance_pruning", None)
    if not key:
        return {"conversation": "", "suggestions": [], "info": {}, "error": "no conversation seen yet"}
    messages = _DROP.seen_full(key)
    if not messages:
        return {
            "conversation": key,
            "suggestions": [],
            "info": {},
            "error": "no messages recorded for this conversation yet",
        }

    keep_recent = int(getattr(cfg, "keep_recent", 3) if cfg else 3)
    mode = str(getattr(cfg, "mode", "judge") if cfg else "judge")
    arbitration = str(getattr(cfg, "arbitration", "safest") if cfg else "safest")
    drop_t = float(getattr(cfg, "drop_threshold", 0.35) if cfg else 0.35)
    summ_t = float(getattr(cfg, "summarize_threshold", 0.6) if cfg else 0.6)
    judge_model = (getattr(cfg, "judge_model", None) if cfg else None) or _SETTINGS.judge_model
    encoder_path = (getattr(cfg, "encoder_path", None) if cfg else None) or _SETTINGS.encoder_path

    need_judge = mode in ("judge", "ensemble")
    need_encoder = mode in ("encoder", "ensemble")

    judge = _pick_summariser(judge_model) if need_judge else None
    if need_judge and judge is None and mode == "judge":
        return {
            "conversation": key,
            "suggestions": [],
            "info": {},
            "error": "no upstream API key configured for the auditor",
        }
    encoder = _get_encoder(encoder_path, drop_t, summ_t) if need_encoder else None
    if need_encoder and encoder is None and mode == "encoder":
        return {
            "conversation": key,
            "suggestions": [],
            "info": {},
            "error": "relevance encoder could not be loaded",
        }

    suggestions, info, episodes = suggest_removals(
        messages,
        keep_recent=keep_recent,
        mode=mode,
        arbitration=arbitration,
        judge_client=judge,
        encoder=encoder,
        return_episodes=True,
    )
    if bool(getattr(cfg, "feedback_logging", True)):
        try:
            record_audit(
                build_audit_rows(
                    episodes, suggestions,
                    task=active_task(messages), conv=key, surface="gateway",
                )
            )
        except Exception as e:  # pragma: no cover - defensive
            print(f"[acm-gateway] audit log failed: {e!r}", flush=True)
    out: List[Dict[str, Any]] = []
    for s in suggestions:
        member_fps = [
            fingerprint(messages[i]) for i in s.member_indices if 0 <= i < len(messages)
        ]
        dropped = bool(member_fps) and all(_DROP.is_dropped(key, fp) for fp in member_fps)
        out.append({**s.to_dict(), "member_fps": member_fps, "dropped": dropped})
    _record([{"type": "relevance_suggestions", "conversation": key,
              "candidates": info.get("candidates", 0), "drop": info.get("drop", 0),
              "summarize": info.get("summarize", 0)}])
    return {"conversation": key, "suggestions": out, "info": info}


@app.post("/relevance/feedback")
async def relevance_feedback(request: Request) -> Any:
    """Log the user's decision on a suggestion (accept/reject/edit) to the
    feedback JSONL — the dataset the encoder re-train + judge DPO read later.
    Body: ``{conv?, episode_id, shown_label, user_action, final_label, ...}``."""
    body: Dict[str, Any] = await request.json()
    record = {
        "conv": body.get("conv") or _DROP.latest_conversation() or "",
        "episode_id": body.get("episode_id"),
        "title": body.get("title"),
        "shown_label": body.get("shown_label"),
        "user_action": body.get("user_action"),
        "final_label": body.get("final_label"),
        "score": body.get("score"),
        "source": body.get("source"),
        "tokens": body.get("tokens"),
        "surface": "gateway",
    }
    try:
        path = record_feedback(record)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return JSONResponse({"ok": True, "logged_to": str(path)})


@app.post("/relevance/summarize")
async def relevance_summarize(request: Request) -> Any:
    """Replace an episode with a short summary instead of dropping it — saves
    tokens while keeping the gist. Drops the episode's messages and injects one
    summary note on every future request. Body: ``{conv?, member_fps, title?,
    model?}``."""
    body: Dict[str, Any] = await request.json()
    fps = body.get("member_fps") or body.get("fps") or []
    if not isinstance(fps, list) or not fps:
        return JSONResponse(
            {"error": "missing 'member_fps' (non-empty list)"}, status_code=400
        )
    conv = body.get("conv") or _DROP.latest_conversation()
    if not conv:
        return JSONResponse({"error": "no conversation known yet"}, status_code=400)

    fp_set = {str(f) for f in fps}
    members = [m for m in _DROP.seen_full(conv) if fingerprint(m) in fp_set]
    if not members:
        return JSONResponse(
            {"error": "no matching messages for this episode"}, status_code=404
        )

    summariser = _pick_summariser(body.get("model"))
    if summariser is None:
        return JSONResponse(
            {"error": "no upstream API key configured for summarization"},
            status_code=503,
        )

    transcript = "\n".join(
        f"[{getattr(m, 'type', None) or m.__class__.__name__}] "
        f"{_norm_text(getattr(m, 'content', ''))[:2000]}"
        for m in members
    )[:12000]
    try:
        resp = summariser.invoke(
            [
                SystemMessage(content=DEFAULT_SUMMARY_SYSTEM),
                HumanMessage(
                    content="Summarise this finished portion of the "
                    f"conversation:\n\n{transcript}"
                ),
            ]
        )
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)
    summary = resp.content if isinstance(resp.content, str) else str(resp.content)
    summary = (summary or "").strip()
    if not summary:
        return JSONResponse(
            {"error": "summariser returned empty text"}, status_code=502
        )

    title = (body.get("title") or "earlier step").strip()
    note = f"[Summary of earlier step — {title}]\n{summary}"
    _DROP.summarize(conv, [str(f) for f in fps], note)
    _record(
        [{"type": "relevance_summarize", "conversation": conv, "members": len(fps)}]
    )
    return JSONResponse(
        {
            "ok": True,
            "conversation": conv,
            "summary": note,
            "dropped": _DROP.dropped(conv),
        }
    )


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

    incoming = body.get("messages", []) or []
    lc_messages = openai_to_lc(incoming)

    # Resolve the chat (context window) + its per-chat profile before anything,
    # scoped to the project (cwd) the client is running in.
    conv = _conv_key(request, lc_messages, body)
    proj = project_path(lc_messages)
    profile = _resolve_profile(conv)
    _WINDOWS.ensure(conv, project=proj)

    # Manual removal: strip any tombstoned messages before anything else.
    lc_messages = _apply_droplist(conv, lc_messages)
    _WINDOWS.touch(
        conv,
        title=_DROP.title(conv),
        tokens=_DROP.context_tokens(conv),
        messages=len(_DROP.seen(conv)),
        project=proj,
    )

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

    new_messages, pipeline_events = run_pipeline(
        lc_messages,
        profile,
        summariser=summariser,
        visual_cfg=load_visual_cfg(active_config_path()),
    )
    if profile.context_management.summarization.enabled and summariser is None:
        pipeline_events.append(
            {
                "type": "notice",
                "level": "warn",
                "step": "summarization",
                "message": "Summarization is on but was skipped — no upstream API key configured. Add one in Providers.",
            }
        )
    _record(pipeline_events)

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
    # Snapshot exactly what we forward (post-pipeline) for the Context Window view.
    _DROP.record_sent(
        conv,
        surface="openai",
        model=body.get("model"),
        system=None,
        messages=body.get("messages"),
        tools=body.get("tools"),
    )
    events.publish({"type": "turn", "conv": conv, "project": proj})
    up = GenericUpstream(target.url, target.headers or {})
    if body.get("stream"):
        return StreamingResponse(
            up.chat_stream(body), media_type="text/event-stream"
        )
    try:
        data = await up.chat(body)
    except httpx.HTTPStatusError as e:
        # Surface the upstream's real status + body instead of a bare 500, so the
        # IDE shows the actual reason (mirrors the /v1/messages path).
        try:
            payload = e.response.json()
        except Exception:
            payload = {"error": {"message": e.response.text[:1000]}}
        return JSONResponse(payload, status_code=e.response.status_code)
    return JSONResponse(data)


@app.post("/v1/messages")
async def anthropic_messages(request: Request) -> Any:
    """Anthropic-native surface (Claude Code). Same pipeline as the OpenAI path,
    using the Messages-API translator + an Anthropic upstream."""
    body: Dict[str, Any] = await request.json()

    lc_messages = anthropic_to_lc(body.get("system"), body.get("messages", []) or [])

    # Snapshot the client's original system block + the identity of its system
    # messages *before* the pipeline runs. In OAuth/subscription passthrough the
    # system must be forwarded verbatim (the endpoint validates Claude Code's
    # identity prompt), and any system note the pipeline injects (e.g. a summary)
    # has to be re-homed into the message array rather than that block.
    orig_system = body.get("system")
    orig_system_ids = {id(m) for m in lc_messages if isinstance(m, SystemMessage)}

    # Which chat is this? Resolve its context window + per-chat profile, and
    # auto-create the window on first sight (inheriting the global default),
    # scoped to the project (cwd) Claude Code is running in.
    conv = _conv_key(request, lc_messages, body)
    proj = project_path(lc_messages)
    profile = _resolve_profile(conv)
    _WINDOWS.ensure(conv, project=proj)

    # Manual removal: strip tombstoned messages before the pipeline.
    lc_messages = _apply_droplist(conv, lc_messages)
    _WINDOWS.touch(
        conv,
        title=_DROP.title(conv),
        tokens=_DROP.context_tokens(conv),
        messages=len(_DROP.seen(conv)),
        project=proj,
    )

    summariser = None
    if (
        profile.context_management.summarization.enabled
        and _SETTINGS.anthropic_api_key
    ):
        # The summariser is a separate model call needing its own key — it can't
        # ride the client's single-purpose OAuth token, so it only runs when an
        # x-api-key is configured (subscription-only setups skip summarisation).
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

    new_messages, pipeline_events = run_pipeline(
        lc_messages,
        profile,
        summariser=summariser,
        visual_cfg=load_visual_cfg(active_config_path()),
    )
    if profile.context_management.summarization.enabled and summariser is None:
        pipeline_events.append(
            {
                "type": "notice",
                "level": "warn",
                "step": "summarization",
                "message": "Summarization is on but was skipped — no upstream API key configured. Add one in Providers.",
            }
        )
    _record(pipeline_events)

    # Route to a configured Anthropic provider if one is selected, else env,
    # then decide whether to forward the client's own subscription bearer.
    target = _resolve(body.get("model", ""), request)
    use_passthrough, auth_header = _anthropic_auth_decision(request, target)

    # In passthrough mode, re-home any pipeline-injected system note (a summary)
    # into the message array so its content survives without overwriting Claude
    # Code's identity prompt — which `lc_to_anthropic` would otherwise merge into
    # the system block and trip the OAuth endpoint's validation.
    if use_passthrough:
        new_messages = [
            HumanMessage(content=m.content, id=getattr(m, "id", None))
            if isinstance(m, SystemMessage) and id(m) not in orig_system_ids
            else m
            for m in new_messages
        ]

    new_system, new_msgs = lc_to_anthropic(new_messages)
    body = dict(body)
    body["messages"] = new_msgs
    if use_passthrough:
        # Forward the client's original system byte-for-byte (OAuth invariant).
        if orig_system is not None:
            body["system"] = orig_system
        elif "system" in body:
            del body["system"]
    elif new_system is not None:
        body["system"] = new_system
    elif "system" in body:
        del body["system"]

    if target.kind == "anthropic":
        up = AnthropicUpstream(
            target.base_url, target.api_key, _SETTINGS.anthropic_version
        )
        body["model"] = target.model
    else:
        up = _anthropic_upstream()

    passthrough_headers = _passthrough_headers(request) if use_passthrough else None

    # Record which credential this turn used, for the /status HUD.
    global _LAST_AUTH
    _LAST_AUTH = {
        "mode": "passthrough" if use_passthrough else "api_key",
        "subscription": bool(use_passthrough),
        "token_tail": (
            auth_header.strip()[-4:] if use_passthrough and auth_header else None
        ),
    }

    # Snapshot exactly what we forward (post-pipeline) for the Context Window view.
    _DROP.record_sent(
        conv,
        surface="anthropic",
        model=body.get("model"),
        system=body.get("system"),
        messages=body.get("messages"),
        tools=body.get("tools"),
    )
    events.publish({"type": "turn", "conv": conv, "project": proj})

    # Pass through Claude Code's beta opt-ins: the `anthropic-beta` header and
    # the `?beta=true` endpoint selector. Stripping these makes the upstream 400
    # on otherwise-valid requests. In api-key mode we drop the auth-specific
    # ``oauth-*`` betas (they'd be rejected against an x-api-key); in passthrough
    # mode we forward the client's own bearer, so we keep them.
    beta = request.headers.get("anthropic-beta")
    if beta and not use_passthrough:
        beta = ",".join(
            b.strip()
            for b in beta.split(",")
            if b.strip() and not b.strip().startswith("oauth")
        ) or None
    beta_query = request.query_params.get("beta") in ("true", "1")

    if body.get("stream"):
        return StreamingResponse(
            up.messages_stream(
                body,
                beta=beta,
                beta_query=beta_query,
                auth_header=auth_header,
                passthrough_headers=passthrough_headers,
            ),
            media_type="text/event-stream",
        )
    try:
        data = await up.messages(
            body,
            beta=beta,
            beta_query=beta_query,
            auth_header=auth_header,
            passthrough_headers=passthrough_headers,
        )
    except httpx.HTTPStatusError as e:
        # Surface the upstream's real status + body instead of a bare 500, so
        # the IDE stops retrying blindly and shows the actual reason.
        try:
            payload = e.response.json()
        except Exception:
            payload = {"error": {"message": e.response.text[:1000]}}
        return JSONResponse(payload, status_code=e.response.status_code)
    return JSONResponse(data)
