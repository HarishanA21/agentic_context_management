"""The visual method — rasterise large tool outputs into an image.

The website renders a big tool output to a PNG so the model reads it as an image
(token-efficient, and for some content more accurate), while keeping every URL /
citation / id as text so nothing citeable is lost. This is the gateway port: it
operates on the **tool result messages already in the request**, swapping a big
text result for ``[references] + image`` content before the model sees it.

We use only the framework-free pieces of the engine's visual pipeline
(``render_2col`` + the reference indexer), so there's no LLM call and no coupling
to the website's tools — just rasterise the raw output and extract citations.
Mirrors ``visual_tool.compressor._pack_message``'s output shape exactly, so the
image-recall eviction / caching steps downstream treat these images like any
other.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import BaseMessage, ToolMessage

log = logging.getLogger("acm.visual")


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _content_has_image(content: Any) -> bool:
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") in {"image", "image_url"} for b in content
    )


def _rasterise(text: str, tool_name: str) -> Optional[Tuple[list, str]]:
    """Return ``(multimodal_content, digest)`` for a text blob, or None if the
    rasteriser/indexer is unavailable (then we leave the message untouched)."""
    try:
        from visual_tool.rasterizer import render_2col
        from visual_tool.indexer import build_references_block, extract_references
    except Exception as e:  # Pillow missing / vendored module absent
        log.warning("[visual] rasteriser unavailable: %r — skipping", e)
        return None

    refs = extract_references(text)
    png = render_2col(text)
    b64 = base64.b64encode(png).decode("ascii")
    refs_block = build_references_block(refs) or (
        f"(tool: {tool_name} — see image for full output; nothing to cite verbatim)"
    )
    digest = " ".join((text or "").split())[:240]
    content = [
        {"type": "text", "text": refs_block},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
    ]
    return content, digest


def visualize_tool_messages(
    messages: List[BaseMessage],
    *,
    trigger_tokens: int = 500,
    only_tools: Optional[set] = None,
    exclude_tools: Optional[set] = None,
) -> Tuple[List[BaseMessage], Dict[str, Any]]:
    """Replace the body of each large, plain-text ToolMessage with a rasterised
    image + references. Skips already-multimodal results and respects the
    only/exclude tool filters. Returns ``(replacements, info)`` using the same
    id-preserving contract as the other techniques."""
    info: Dict[str, Any] = {"rasterised": 0, "freed_tokens": 0, "total_tool_msgs": 0}
    excl = exclude_tools or set()

    replacements: List[BaseMessage] = []
    for m in messages:
        if not isinstance(m, ToolMessage):
            continue
        info["total_tool_msgs"] += 1
        name = getattr(m, "name", "") or ""
        content = getattr(m, "content", "")
        if _content_has_image(content) or not isinstance(content, str):
            continue  # already an image / multimodal — leave it
        if only_tools and name not in only_tools:
            continue
        if name in excl:
            continue
        if _estimate_tokens(content) < trigger_tokens:
            continue

        out = _rasterise(content, name)
        if out is None:
            continue
        new_content, digest = out
        before = _estimate_tokens(content)
        new_msg = ToolMessage(
            content=new_content,
            tool_call_id=getattr(m, "tool_call_id", "") or "",
            name=getattr(m, "name", None),
            id=getattr(m, "id", None),  # same id ⇒ in-place replace
            additional_kwargs={"image_digest": digest} if digest else {},
        )
        # text refs are tiny; the image is sent as base64 but counts very
        # differently against the budget — report the text tokens freed.
        info["freed_tokens"] += max(0, before - _estimate_tokens(new_content[0]["text"]))
        info["rasterised"] += 1
        replacements.append(new_msg)

    return replacements, info
