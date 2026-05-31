"""Public entry point: ``maybe_compress(tool_name, output, mode)``.

Decides whether a tool's output is big enough to compress, routes it
through the right formatter (``auxiliary`` LLM-generated or
``templated`` hand-written), runs the rasteriser, and packages a
multimodal ``ToolMessage``.

When the output is under ``threshold_tokens`` (~500 by default), this
just returns the raw string — image overhead isn't worth it for small
outputs.

Used by the Strategy Demo's "Visual Compression Bench" tab columns 2
and 4. Main /chat flow is unaffected.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Iterable, List, Optional, Tuple, Union

from langchain_core.messages import ToolMessage

from .auxiliary_llm import format_via_aux_async, format_via_aux_sync
from .indexer import build_references_block, extract_references
from .rasterizer import render_2col
from .templates import format_for_tool, has_template


log = logging.getLogger(__name__)


# Below this size, image overhead (~5-15 KB of base64) outweighs any
# token savings. Tunable per request via the `threshold_tokens` arg.
DEFAULT_THRESHOLD_TOKENS = 500


def _estimate_tokens(text: str) -> int:
    """Cheap ~4-chars-per-token estimator — same as api._estimate_tokens
    so the per-message accounting in the Context panel lines up."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _pack_message(
    tool_name: str,
    formatted_text: str,
    refs: List[Tuple[str, str]],
    *,
    tool_call_id: str,
) -> ToolMessage:
    """Build the multimodal ToolMessage payload (text REFERENCES +
    base64 image). Every provider adapter we ship serialises this
    shape — OpenAI/Azure/OpenRouter as `image_url`, Anthropic and
    Bedrock-Claude as `image` source blocks."""
    png_bytes = render_2col(formatted_text)
    b64 = base64.b64encode(png_bytes).decode("ascii")
    refs_block = build_references_block(refs)
    text_part = refs_block or (
        f"(tool: {tool_name} — see image for full output; nothing to cite verbatim)"
    )
    return ToolMessage(
        tool_call_id=tool_call_id or "",
        name=tool_name,
        content=[
            {"type": "text", "text": text_part},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{b64}",
                },
            },
        ],
    )


# ─── sync entry ─────────────────────────────────────────────────────────


def maybe_compress(
    tool_name: str,
    output: str,
    *,
    mode: str = "auxiliary",
    threshold_tokens: int = DEFAULT_THRESHOLD_TOKENS,
    tool_call_id: str = "",
) -> Union[ToolMessage, str]:
    """Compress a tool output into a multimodal ToolMessage, or pass
    through unchanged when it isn't worth the overhead.

    Args:
        tool_name: the tool's registered name. Used for template lookup
            (``templated`` mode) and as the cache key (``auxiliary`` mode).
        output: the raw text the tool returned.
        mode: ``"auxiliary"`` (default — Column 2 of the demo) routes
            through the LLM-generated template path. ``"templated"``
            (Column 4) routes through the hand-written templates and
            silently falls back to ``"auxiliary"`` for any tool that
            doesn't have a hand-written template.
        threshold_tokens: below this, return the raw string. Default 500.
        tool_call_id: the matching ``tool_use`` id from the AIMessage.
            Required by every provider adapter when the result is
            multimodal — pass it through verbatim from the tool runner.

    Returns:
        - ``ToolMessage`` (multimodal) when compression fired.
        - ``str`` (unchanged) when below threshold.
    """
    est = _estimate_tokens(output)
    if est < threshold_tokens:
        return output

    mode = (mode or "auxiliary").lower()
    formatted, refs = _format(tool_name, output, mode)
    return _pack_message(
        tool_name, formatted, refs, tool_call_id=tool_call_id
    )


def _format(
    tool_name: str, output: str, mode: str
) -> Tuple[str, List[Tuple[str, str]]]:
    """Sync formatter dispatch — picks the right path and falls back
    gracefully when a template / aux LLM call misbehaves."""
    if mode == "templated":
        result = format_for_tool(tool_name, output)
        if result is not None:
            return result
        # Defensive: fall through to aux if a tool we forgot to template
        # somehow ends up in Column 4. Better than erroring out the run.
        log.debug(
            "[visual_tool] no template for %r; falling back to aux", tool_name
        )
    # auxiliary (or templated-fallback)
    try:
        return format_via_aux_sync(tool_name, output)
    except Exception as e:
        log.warning(
            "[visual_tool] aux formatting failed for %r: %r — using raw", tool_name, e
        )
        return output, extract_references(output)


# ─── async entry (preferred when caller is already async) ──────────────


async def maybe_compress_async(
    tool_name: str,
    output: str,
    *,
    mode: str = "auxiliary",
    threshold_tokens: int = DEFAULT_THRESHOLD_TOKENS,
    tool_call_id: str = "",
) -> Union[ToolMessage, str]:
    """Async-friendly version. Use this from async call sites (the demo
    handler) so we don't spawn a worker thread just to bridge back to
    asyncio.run for the LLM call."""
    est = _estimate_tokens(output)
    if est < threshold_tokens:
        return output

    mode = (mode or "auxiliary").lower()
    if mode == "templated":
        result = format_for_tool(tool_name, output)
        if result is not None:
            formatted, refs = result
            return _pack_message(
                tool_name, formatted, refs, tool_call_id=tool_call_id
            )
        log.debug(
            "[visual_tool] no template for %r; falling back to aux (async)",
            tool_name,
        )
    try:
        formatted, refs = await format_via_aux_async(tool_name, output)
    except Exception as e:
        log.warning(
            "[visual_tool] aux formatting failed for %r: %r — using raw",
            tool_name, e,
        )
        formatted, refs = output, extract_references(output)
    return _pack_message(
        tool_name, formatted, refs, tool_call_id=tool_call_id
    )
