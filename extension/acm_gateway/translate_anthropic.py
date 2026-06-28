"""Translate between the Anthropic Messages API wire format and LangChain.

This is the sibling of ``translate.py`` (OpenAI). The Anthropic shape differs in
three ways that matter for the techniques:

  * ``system`` is **top-level**, not a message in the array.
  * Tool *results* arrive as ``tool_result`` blocks inside a **user** turn;
    tool *calls* are ``tool_use`` blocks inside an **assistant** turn.
  * Images are ``{"type": "image", "source": {...}}`` blocks — which the
    engine's ``evict_stale_images`` already recognises (it matches ``image`` /
    ``image_url``), and ``cache_control`` lives natively on content blocks.

Strategy: on the way IN we *split* each ``tool_result`` block into its own
``ToolMessage`` so trimming / eviction can act on results individually. On the
way OUT we *merge* consecutive same-role messages back into one Anthropic turn
(the API requires strict user/assistant alternation) and prune orphaned
tool_result blocks left behind when summarisation drops an old assistant turn.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)


# ─── inbound: Anthropic -> LangChain ─────────────────────────────────────


def _blocks_to_text(blocks: Any) -> str:
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return str(blocks or "")
    parts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p)


def _simplify(blocks: List[Dict[str, Any]]) -> Any:
    """If every block is plain text, collapse to a string; otherwise keep the
    list (images / cache markers must survive)."""
    if all(isinstance(b, dict) and b.get("type") == "text" for b in blocks):
        return "\n".join(b.get("text", "") for b in blocks)
    return blocks


def anthropic_to_lc(
    system: Optional[Any], messages: List[Dict[str, Any]]
) -> List[BaseMessage]:
    """Anthropic ``system`` + ``messages`` -> LangChain messages with stable ids."""
    out: List[BaseMessage] = []
    idx = 0

    if system:
        out.append(SystemMessage(content=_to_system_content(system), id=f"a{idx}"))
        idx += 1

    for m in messages:
        role = m.get("role")
        content = m.get("content")
        blocks = content if isinstance(content, list) else [
            {"type": "text", "text": content or ""}
        ]

        if role == "user":
            human_blocks: List[Dict[str, Any]] = []
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tc = b.get("content")
                    out.append(
                        ToolMessage(
                            content=tc if tc is not None else "",
                            tool_call_id=b.get("tool_use_id", "") or "",
                            id=f"a{idx}",
                        )
                    )
                    idx += 1
                else:
                    human_blocks.append(b)
            if human_blocks:
                out.append(HumanMessage(content=_simplify(human_blocks), id=f"a{idx}"))
                idx += 1

        elif role == "assistant":
            text_parts: List[str] = []
            tool_calls: List[Dict[str, Any]] = []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    text_parts.append(b.get("text", ""))
                elif b.get("type") == "tool_use":
                    tool_calls.append(
                        {
                            "id": b.get("id", ""),
                            "name": b.get("name", ""),
                            "args": b.get("input", {}) or {},
                        }
                    )
            out.append(
                AIMessage(
                    content="\n".join(p for p in text_parts if p),
                    tool_calls=tool_calls,
                    id=f"a{idx}",
                )
            )
            idx += 1
        else:  # unknown role — keep as user text so nothing is lost
            out.append(HumanMessage(content=_simplify(blocks), id=f"a{idx}"))
            idx += 1

    return out


def _to_system_content(system: Any) -> Any:
    """Normalise Anthropic ``system`` (string or block list) for a SystemMessage.
    Block lists are kept so any ``cache_control`` survives the round-trip."""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return system
    return str(system or "")


# ─── outbound: LangChain -> Anthropic ────────────────────────────────────


def _data_url_to_anthropic_image(url: str) -> Optional[Dict[str, Any]]:
    """Convert a ``data:image/png;base64,XXXX`` URL to an Anthropic image block.
    Returns None for non-data URLs (remote URLs aren't valid in this position)."""
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    try:
        header, b64 = url.split(",", 1)
        media_type = header[5:].split(";", 1)[0] or "image/png"
    except ValueError:
        return None
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": b64},
    }


def _normalise_image_blocks(blocks: Any) -> Any:
    """Map OpenAI-style ``image_url`` blocks (which the visual method emits) to
    Anthropic ``image``/``source`` blocks. Leaves text and already-Anthropic
    blocks untouched."""
    if not isinstance(blocks, list):
        return blocks
    out: List[Any] = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "image_url":
            url = (b.get("image_url") or {}).get("url", "")
            conv = _data_url_to_anthropic_image(url)
            out.append(conv if conv is not None else b)
        else:
            out.append(b)
    return out


def _tool_result_content(content: Any) -> Any:
    """Anthropic ``tool_result.content`` accepts a string or a block list. The
    engine may have rewritten this to a placeholder string (trim), a de-imaged
    block list (evict), or a rasterised image (visual method). Normalise any
    OpenAI-style image blocks to Anthropic's shape."""
    if content is None:
        return ""
    return _normalise_image_blocks(content)


def _ai_blocks(m: AIMessage) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    content = getattr(m, "content", "")
    if isinstance(content, list):
        blocks.extend(b for b in content if isinstance(b, dict))
    elif content:
        blocks.append({"type": "text", "text": content})
    for tc in getattr(m, "tool_calls", None) or []:
        blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": tc.get("name", ""),
                "input": tc.get("args", {}) or {},
            }
        )
    return blocks


def _user_blocks(m: BaseMessage) -> List[Dict[str, Any]]:
    if isinstance(m, ToolMessage):
        return [
            {
                "type": "tool_result",
                "tool_use_id": getattr(m, "tool_call_id", "") or "",
                "content": _tool_result_content(getattr(m, "content", "")),
            }
        ]
    content = getattr(m, "content", "")
    if isinstance(content, list):
        return _normalise_image_blocks([b for b in content if isinstance(b, dict)])
    return [{"type": "text", "text": content or ""}]


def lc_to_anthropic(
    messages: List[BaseMessage],
) -> Tuple[Optional[Any], List[Dict[str, Any]]]:
    """LangChain messages -> ``(system, messages)`` in Anthropic shape.

    Merges consecutive same-role messages (alternation requirement + tool_result
    grouping) and prunes orphan tool_result blocks.
    """
    system_blocks: List[Dict[str, Any]] = []
    seq: List[Tuple[str, BaseMessage]] = []

    for m in messages:
        if isinstance(m, SystemMessage):
            c = getattr(m, "content", "")
            if isinstance(c, list):
                system_blocks.extend(b for b in c if isinstance(b, dict))
            elif c:
                system_blocks.append({"type": "text", "text": c})
            continue
        role = "assistant" if isinstance(m, AIMessage) else "user"
        seq.append((role, m))

    # Merge consecutive same-role runs into one Anthropic turn.
    merged: List[List[Any]] = []
    for role, m in seq:
        if merged and merged[-1][0] == role:
            merged[-1][1].append(m)
        else:
            merged.append([role, [m]])

    out_messages: List[Dict[str, Any]] = []
    for role, group in merged:
        blocks: List[Dict[str, Any]] = []
        for m in group:
            blocks.extend(_ai_blocks(m) if role == "assistant" else _user_blocks(m))
        out_messages.append({"role": role, "content": blocks})

    out_messages = _prune_orphan_tool_results(out_messages)
    system = _finalise_system(system_blocks)
    return system, out_messages


def _prune_orphan_tool_results(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Drop ``tool_result`` blocks whose ``tool_use_id`` has no matching
    ``tool_use`` anywhere (summarisation/sliding-window can remove the assistant
    turn that issued the call). Drop now-empty user turns. Anthropic 400s on an
    orphan tool_result, so this keeps the forwarded request valid.

    TODO(acm): also handle orphan *tool_use* (assistant call whose result was
    dropped) — rarer, and only invalid mid-conversation.
    """
    known_ids = set()
    for m in messages:
        for b in m.get("content", []):
            if isinstance(b, dict) and b.get("type") == "tool_use":
                known_ids.add(b.get("id"))

    cleaned: List[Dict[str, Any]] = []
    for m in messages:
        if m["role"] != "user":
            cleaned.append(m)
            continue
        kept = [
            b
            for b in m.get("content", [])
            if not (
                isinstance(b, dict)
                and b.get("type") == "tool_result"
                and b.get("tool_use_id") not in known_ids
            )
        ]
        if kept:
            cleaned.append({"role": "user", "content": kept})
    return cleaned


def _finalise_system(blocks: List[Dict[str, Any]]) -> Optional[Any]:
    if not blocks:
        return None
    # Plain single text block with no cache marker -> a string (simplest form).
    if len(blocks) == 1 and blocks[0].get("type") == "text" and "cache_control" not in blocks[0]:
        return blocks[0].get("text", "")
    return blocks
