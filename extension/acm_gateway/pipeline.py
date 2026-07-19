"""The gateway's context-editing orchestrator.

This is the wire-level twin of ``context_editing.apply_context_edits``. That
function drives a LangGraph agent (``agent.get_state`` / ``update_state``); we
don't have one — we have a list of messages off the HTTP request. So we call the
same *pure* technique functions in the **same fixed order** and apply their
id-keyed rewrites to the list ourselves:

    1. tool_result_trimming   (mechanical, no LLM)
    2. image eviction         (mechanical, no LLM)  — when image_recall evicts
    3. summarization          (one extra LLM call)  — needs a summariser
    4. sliding_window         (dumb fallback, no LLM)
    5. cache breakpoints      (annotate the settled prefix; no output change)

Every step is defensive: a failure in one technique logs and is skipped, never
breaks the request.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from langchain_core.messages import BaseMessage, RemoveMessage, SystemMessage

from acm_engine import (
    Profile,
    annotate_cache_breakpoints,
    evict_stale_images,
    sliding_window_trim,
    summarise_old_messages,
    trim_tool_results,
)


def _ids(msgs: List[Any]) -> List[Optional[str]]:
    """Message ids for event attribution (timeline diffing)."""
    return [getattr(m, "id", None) for m in msgs]


def _apply_replacements(
    messages: List[BaseMessage], replacements: List[BaseMessage]
) -> List[BaseMessage]:
    """Swap messages in place by id (trim / evict contract)."""
    by_id = {getattr(r, "id", None): r for r in replacements}
    return [by_id.get(getattr(m, "id", None), m) for m in messages]


def _apply_removes(
    messages: List[BaseMessage], removes: List[RemoveMessage]
) -> List[BaseMessage]:
    """Drop messages whose id appears in a RemoveMessage list."""
    dead = {getattr(r, "id", None) for r in removes}
    return [m for m in messages if getattr(m, "id", None) not in dead]


def _est_tokens(messages: List[Any]) -> int:
    """Rough whole-list token estimate for the Savings "before/after" table.

    Mirrors the ``len(text) // 4`` heuristic used elsewhere in the gateway
    (``visualizer._estimate_tokens``); an image block counts as a flat 400
    chars so techniques that swap text for images aren't scored as free.
    """
    total = 0
    for m in messages:
        content = getattr(m, "content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    total += len(str(b.get("text", "")))
                elif isinstance(b, dict) and b.get("type") in ("image_url", "image"):
                    total += 400
    return max(0, total // 4)


def run_pipeline(
    messages: List[BaseMessage],
    profile: Profile,
    *,
    summariser: Optional[Any] = None,
    visual_cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[List[BaseMessage], List[Dict[str, Any]]]:
    """Apply every enabled technique and return ``(new_messages, events)``.

    ``summariser`` is an object with ``.invoke(list[BaseMessage]) -> resp`` where
    ``resp.content`` is the summary text (the gateway passes a small upstream
    client). When ``None``, summarization is skipped even if enabled.

    ``visual_cfg`` is the gateway-specific visual-method block (rasterise big
    tool outputs to images). When enabled it runs *first*, so the image-recall
    and trimming steps see the rasterised view.
    """
    events: List[Dict[str, Any]] = []

    def _fail(step: str, e: Exception) -> None:
        """A technique errored: log it *and* surface it to the UI as a notice."""
        _warn(step, e)
        events.append(
            {"type": "notice", "level": "error", "step": step, "message": f"{step} failed: {e!s}"}
        )

    cm = getattr(profile, "context_management", None)
    if cm is None:
        return messages, events

    ir = getattr(cm, "image_recall", None)
    ir_evicts = bool(ir is not None and getattr(ir, "eviction_enabled", False))

    # 0. visual method (rasterise big tool outputs) ---------------------------
    visual_on = bool(visual_cfg and visual_cfg.get("enabled"))
    if visual_on:
        try:
            from .visualizer import visualize_tool_messages

            repl, info = visualize_tool_messages(
                messages,
                trigger_tokens=int(visual_cfg.get("trigger_tokens", 500) or 500),
                only_tools=set(visual_cfg.get("only_tools") or []),
                exclude_tools=set(visual_cfg.get("exclude_tools") or []),
                skip_fps=visual_cfg.get("skip_fps") or None,
            )
            if repl:
                messages = _apply_replacements(messages, repl)
                events.append({"type": "visual_method", "replaced_ids": _ids(repl), **info})
        except Exception as e:  # pragma: no cover - defensive
            _fail("visual_method", e)

    # 1. tool_result_trimming -------------------------------------------------
    # Once images exist (visual method or image-recall eviction), trimming must
    # leave image-bearing messages to the eviction step instead of clobbering
    # them with a bare placeholder.
    trim = getattr(cm, "tool_result_trimming", None)
    if trim is not None and getattr(trim, "enabled", False):
        try:
            before_tok = _est_tokens(messages)
            repl, info = trim_tool_results(
                messages,
                trigger_tokens=trim.trigger_tokens,
                keep_recent=trim.keep_recent,
                exclude_tools=set(trim.exclude_tools or []),
                exclude_images=ir_evicts or visual_on,
            )
            if repl:
                messages = _apply_replacements(messages, repl)
                after_tok = _est_tokens(messages)
                events.append(
                    {
                        "type": "tool_result_trimming",
                        "replaced_ids": _ids(repl),
                        "before_tokens": before_tok,
                        "after_tokens": after_tok,
                        **info,
                    }
                )
        except Exception as e:  # pragma: no cover - defensive
            _fail("tool_result_trimming", e)

    # 2. image eviction (visual recall, accuracy layer) -----------------------
    if ir_evicts:
        try:
            before_tok = _est_tokens(messages)
            repl, info = evict_stale_images(
                messages,
                keep_recent_images=int(getattr(ir, "keep_recent_images", 3) or 3),
            )
            if repl:
                messages = _apply_replacements(messages, repl)
                after_tok = _est_tokens(messages)
                events.append(
                    {
                        "type": "image_eviction",
                        "replaced_ids": _ids(repl),
                        "before_tokens": before_tok,
                        "after_tokens": after_tok,
                        **info,
                    }
                )
        except Exception as e:  # pragma: no cover - defensive
            _fail("image_eviction", e)

    # 3. summarization --------------------------------------------------------
    summ = getattr(cm, "summarization", None)
    if summ is not None and getattr(summ, "enabled", False) and summariser is not None:
        try:
            before_tok = _est_tokens(messages)
            removes, summary_msg, info = summarise_old_messages(
                messages,
                trigger_tokens=summ.trigger_tokens,
                keep_recent=summ.keep_recent,
                summariser_model=summariser,
                instructions=getattr(summ, "instructions", None),
            )
            if summary_msg is not None:
                if getattr(summary_msg, "id", None) is None:
                    summary_msg.id = "summ0"
                messages = [summary_msg] + _apply_removes(messages, removes)
                after_tok = _est_tokens(messages)
                events.append(
                    {
                        "type": "summarization",
                        "removed_ids": _ids(removes),
                        "added_ids": [summary_msg.id],
                        "before_tokens": before_tok,
                        "after_tokens": after_tok,
                        **info,
                    }
                )
        except Exception as e:  # pragma: no cover - defensive
            _fail("summarization", e)

    # 4. sliding_window (dumb safety net) ------------------------------------
    sw = getattr(cm, "sliding_window", None)
    if sw is not None and getattr(sw, "enabled", False):
        try:
            before_tok = _est_tokens(messages)
            removes, info = sliding_window_trim(messages, keep_recent=sw.keep_recent)
            if removes:
                messages = _apply_removes(messages, removes)
                after_tok = _est_tokens(messages)
                events.append(
                    {
                        "type": "sliding_window",
                        "removed_ids": _ids(removes),
                        "before_tokens": before_tok,
                        "after_tokens": after_tok,
                        **info,
                    }
                )
        except Exception as e:  # pragma: no cover - defensive
            _fail("sliding_window", e)

    # 5. cache breakpoints (image_recall caching half) ------------------------
    if ir is not None and getattr(ir, "caching_enabled", False):
        try:
            system = next(
                (m for m in messages if isinstance(m, SystemMessage)), None
            )
            placed = annotate_cache_breakpoints(
                messages, system=system, ttl=getattr(ir, "cache_ttl", "5m")
            )
            if placed:
                events.append({"type": "cache_breakpoints", "placed": placed})
        except Exception as e:  # pragma: no cover - defensive
            _fail("cache_breakpoints", e)

    return messages, events


def _warn(step: str, e: Exception) -> None:
    print(f"[acm-gateway] technique {step} failed: {e!r} — skipped", flush=True)
