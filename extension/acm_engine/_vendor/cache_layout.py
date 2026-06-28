"""Provider-agnostic image-recall middleware (cache + eviction at call time).

This is the *common* solution for the image-recall techniques — it works for
every model the app can route to, because it leans on the one thing all
caching backends share: **a stable, append-only prefix**.

  * OpenAI / Azure (and OpenAI models via OpenRouter): caching is automatic.
    Keeping the prefix stable is all that's needed; the cache_control hints
    we add are simply ignored.
  * Anthropic / Bedrock-Claude / Gemini (incl. via OpenRouter): caching is
    explicit. OpenRouter forwards the ``cache_control`` markers we put on
    content blocks straight through to the upstream provider.

The middleware does both halves of "visual recall", gated by the profile's
``image_recall.mode``:

  * eviction (``evict`` / ``cache_evict``) — inside ``wrap_model_call`` we
    drop the pixels of all but the most-recent K image tool-results from the
    request the model sees, replacing them with their REFERENCES + a digest.
    Doing it here (not just between turns) means it also fires inside a single
    agent tool-loop — which is what the single-shot demo needs.
  * caching (``cache`` / ``cache_evict``) — mark the stable prefix with an
    ephemeral breakpoint so re-reading the frozen history is cheap/fast.
    Caching never changes the model's output.

Gotchas baked in:
  * Breakpoints go on a *text* block, never an image/tool block (Anthropic
    rejects ``cache_control`` there — langchain issue #34920).
  * String content is widened to a one-element list so we can hang a marker.
  * Everything is defensive: an image-recall hiccup must never break a turn.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

log = logging.getLogger("image_recall")


# ─── cache-token reader (used by demo + chat to surface cache details) ───


def read_cache_tokens(msg: BaseMessage) -> Dict[str, int]:
    """Pull cache hit/write counts off an AIMessage, provider-agnostic.

    Returns ``{cache_read, cache_write}``. Anthropic/Gemini expose these via
    ``usage_metadata.input_token_details`` (cache_read / cache_creation);
    OpenAI exposes a ``cache_read`` only. Falls back to ``response_metadata``
    when the normalised field is absent. All-zero when nothing cached.
    """
    out = {"cache_read": 0, "cache_write": 0}
    um = getattr(msg, "usage_metadata", None) or {}
    details = um.get("input_token_details") or {}
    if isinstance(details, dict):
        out["cache_read"] = int(details.get("cache_read") or 0)
        out["cache_write"] = int(
            details.get("cache_creation") or details.get("cache_write") or 0
        )
    if out["cache_read"] == 0 and out["cache_write"] == 0:
        # OpenAI-style raw usage on response_metadata.
        rm = getattr(msg, "response_metadata", None) or {}
        usage = rm.get("usage") or rm.get("token_usage") or {}
        ptd = (usage.get("prompt_tokens_details") or {}) if isinstance(usage, dict) else {}
        if isinstance(ptd, dict):
            out["cache_read"] = int(ptd.get("cached_tokens") or 0)
    return out


# ─── cache breakpoint placement ──────────────────────────────────────────


def _last_stable_index(messages: List[BaseMessage]) -> Optional[int]:
    """Index of the last message in the *settled* prefix (just before the
    final HumanMessage). The newest turn stays dynamic / reprocessed fresh."""
    if len(messages) < 2:
        return None
    last_human = None
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            last_human = i
            break
    boundary = (last_human - 1) if last_human is not None else (len(messages) - 1)
    return boundary if boundary >= 0 else None


def _mark_block(msg: BaseMessage, ttl: str) -> bool:
    """Attach an ephemeral cache_control marker to a *text* block of ``msg``.
    Skips image / non-text blocks. Returns True if a marker was placed."""
    cc: Dict[str, Any] = {"type": "ephemeral"}
    if ttl == "1h":
        cc["ttl"] = "1h"
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        if not content.strip():
            return False
        msg.content = [{"type": "text", "text": content, "cache_control": cc}]
        return True
    if isinstance(content, list):
        for block in reversed(content):
            if isinstance(block, dict) and block.get("type") == "text":
                block["cache_control"] = cc
                return True
    return False


def annotate_cache_breakpoints(
    messages: List[BaseMessage],
    *,
    system: Optional[BaseMessage] = None,
    ttl: str = "5m",
) -> int:
    """Place ephemeral breakpoints on the stable prefix (system + last settled
    message). Mutates in place; returns how many breakpoints were set."""
    placed = 0
    if isinstance(system, SystemMessage) and _mark_block(system, ttl):
        placed += 1
    idx = _last_stable_index(messages)
    if idx is not None:
        target = messages[idx]
        if target.__class__.__name__ == "ToolMessage":
            # Don't mark a bare tool block; walk back to an AI/System text block.
            for j in range(idx, -1, -1):
                if isinstance(messages[j], (AIMessage, SystemMessage)):
                    target = messages[j]
                    break
        if _mark_block(target, ttl):
            placed += 1
    return placed


# ─── the middleware ──────────────────────────────────────────────────────


class ImageRecallMiddleware(AgentMiddleware):
    """Applies the selected image-recall technique on every model call.

    ``mode`` ∈ {off, cache, evict, cache_evict}. Provider-agnostic: caching is
    a no-op effect for auto-caching backends and active for explicit ones.
    Attach only when ``mode != off``.
    """

    def __init__(
        self,
        *,
        mode: str = "off",
        keep_recent_images: int = 3,
        ttl: str = "5m",
    ) -> None:
        super().__init__()
        self.mode = mode if mode in {"off", "cache", "evict", "cache_evict"} else "off"
        self.keep_recent_images = max(1, int(keep_recent_images or 3))
        self.ttl = ttl if ttl in {"5m", "1h"} else "5m"

    @property
    def caching_enabled(self) -> bool:
        return self.mode in {"cache", "cache_evict"}

    @property
    def eviction_enabled(self) -> bool:
        return self.mode in {"evict", "cache_evict"}

    def _apply(self, request) -> None:
        """Mutate request.messages in place: evict stale images, then mark
        cache breakpoints. Order matters — eviction first so the breakpoint
        lands on the de-imaged prefix."""
        msgs = getattr(request, "messages", None)
        if not msgs:
            return
        if self.eviction_enabled:
            # Lazy import avoids a context_editing <-> cache_layout cycle.
            from context_editing import evict_stale_images

            replacements, info = evict_stale_images(
                msgs, keep_recent_images=self.keep_recent_images
            )
            if replacements:
                by_id = {getattr(r, "id", None): r for r in replacements}
                request.messages = [
                    by_id.get(getattr(m, "id", None), m) for m in msgs
                ]
                msgs = request.messages
                log.info(
                    "[image_recall] evicted %d stale image(s), kept %d as pixels, freed ~%d tokens",
                    info["evicted"], info["kept_recent"], info["freed_tokens"],
                )
        if self.caching_enabled:
            placed = annotate_cache_breakpoints(
                msgs,
                system=getattr(request, "system_message", None),
                ttl=self.ttl,
            )
            if placed:
                log.info(
                    "[image_recall] placed %d cache breakpoint(s) (ttl=%s) on the stable prefix",
                    placed, self.ttl,
                )

    def wrap_model_call(self, request, handler):  # type: ignore[override]
        try:
            self._apply(request)
        except Exception as e:  # never break the turn over image-recall
            log.warning("[image_recall] apply failed: %r", e)
        return handler(request)

    async def awrap_model_call(self, request, handler):  # type: ignore[override]
        try:
            self._apply(request)
        except Exception as e:
            log.warning("[image_recall] apply failed: %r", e)
        return await handler(request)


# Back-compat alias (earlier name during development).
CachePrefixMiddleware = ImageRecallMiddleware
