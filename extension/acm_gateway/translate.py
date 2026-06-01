"""Translate between the OpenAI chat wire format and LangChain message objects.

The techniques in ``acm_engine`` operate on ``List[BaseMessage]`` and identify
messages by ``.id`` (the rewrite functions return replacements/removes keyed by
id). So on the way in we assign each message a **stable, position-based id**
(``m0``, ``m1``, …); on the way out we drop those ids again — they're internal.

Coverage: system / user / assistant (incl. ``tool_calls``) / tool messages, and
multimodal user/tool content (text + ``image_url`` blocks), which is what the
image-recall technique needs. Anything exotic is passed through verbatim.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)


def openai_to_lc(messages: List[Dict[str, Any]]) -> List[BaseMessage]:
    """OpenAI ``messages`` array -> LangChain messages with stable ids."""
    out: List[BaseMessage] = []
    for i, m in enumerate(messages):
        mid = f"m{i}"
        role = m.get("role")
        content = m.get("content", "")
        if role == "system":
            out.append(SystemMessage(content=content, id=mid))
        elif role == "user":
            out.append(HumanMessage(content=content, id=mid))
        elif role == "tool":
            out.append(
                ToolMessage(
                    content=content if content is not None else "",
                    tool_call_id=m.get("tool_call_id", "") or "",
                    name=m.get("name"),
                    id=mid,
                )
            )
        elif role == "assistant":
            tool_calls = []
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {}) or {}
                args = fn.get("arguments", "{}")
                if isinstance(args, str):
                    try:
                        args = json.loads(args or "{}")
                    except json.JSONDecodeError:
                        args = {"_raw": args}
                tool_calls.append(
                    {"id": tc.get("id", ""), "name": fn.get("name", ""), "args": args}
                )
            out.append(
                AIMessage(content=content or "", tool_calls=tool_calls, id=mid)
            )
        else:
            # Unknown role — keep it as a human turn so nothing is dropped.
            out.append(HumanMessage(content=content or "", id=mid))
    return out


def lc_to_openai(messages: List[BaseMessage]) -> List[Dict[str, Any]]:
    """LangChain messages -> OpenAI ``messages`` array (ids stripped)."""
    out: List[Dict[str, Any]] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            out.append({"role": "system", "content": _content(m)})
        elif isinstance(m, HumanMessage):
            out.append({"role": "user", "content": _content(m)})
        elif isinstance(m, ToolMessage):
            out.append(
                {
                    "role": "tool",
                    "content": _content(m),
                    "tool_call_id": getattr(m, "tool_call_id", "") or "",
                    **({"name": m.name} if getattr(m, "name", None) else {}),
                }
            )
        elif isinstance(m, AIMessage):
            msg: Dict[str, Any] = {"role": "assistant", "content": _content(m) or None}
            tcs = getattr(m, "tool_calls", None) or []
            if tcs:
                msg["tool_calls"] = [
                    {
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": tc.get("name", ""),
                            "arguments": json.dumps(tc.get("args", {}) or {}),
                        },
                    }
                    for tc in tcs
                ]
            out.append(msg)
        else:  # pragma: no cover - defensive
            out.append({"role": "user", "content": _content(m)})
    return out


def _content(m: BaseMessage) -> Any:
    """Return content untouched — strings stay strings, multimodal block lists
    (text + image_url, possibly carrying ``cache_control`` markers we added)
    stay lists so the upstream provider receives them intact."""
    return getattr(m, "content", "")
