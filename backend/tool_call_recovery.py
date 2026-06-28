"""Recover tool calls that some models emit as TEXT instead of as
structured ``tool_calls``.

Gemini (notably via OpenRouter's OpenAI-compatible endpoint) sometimes
leaks its native function-calling format into the assistant message
*content* instead of returning a structured tool call::

    ```tool_code
    print(default_api.write_project_file(filename="x.txt", content="..."))
    ```

When that happens the agent sees an ``AIMessage`` with empty
``tool_calls`` and simply stops — the user sees the raw call as text and
the tool never runs. This middleware detects the ``default_api.<tool>(…)``
pattern, parses it with ``ast`` (so escaping inside big multiline string
args is handled exactly), and rewrites the message with real
``tool_calls`` so the agent executes them.

It is a no-op when the model already returned structured tool calls or
when no leak pattern is present, so it's safe to attach on every model.
Truncated / unparseable output is left untouched (no regression — the
user just sees what they would have seen anyway).
"""

from __future__ import annotations

import ast
import logging
import re
import uuid
from typing import Any, Dict, List, Optional

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

log = logging.getLogger(__name__)

# The reliable signal is the `default_api.<tool>(` attribute access; the
# ```tool_code fence is sometimes omitted, so we don't depend on it.
_LEAK_RE = re.compile(r"default_api\.\w+\s*\(")


def _text_of(content: Any) -> str:
    """Flatten message content (str or list of blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    return ""


def _iter_default_api_calls(code: str):
    """Yield ``ast.Call`` nodes of the form ``default_api.<attr>(...)``.

    Tolerant of surrounding prose: tries a full parse first, then a
    carved-out ``default_api…)`` slice if the whole snippet won't parse.
    """
    snippets = [code]
    start = code.find("default_api.")
    if start != -1:
        end = code.rfind(")")
        if end > start:
            snippets.append(code[start : end + 1])
    for snip in snippets:
        try:
            tree = ast.parse(snip.strip())
        except Exception:
            continue
        found = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "default_api"
            ):
                found = True
                yield node
        if found:
            return  # first snippet that parses cleanly wins


def extract_leaked_calls(text: str) -> List[Dict[str, Any]]:
    """Parse every ``default_api.<tool>(<kwargs>)`` call out of ``text``
    into LangChain tool_call dicts. Empty list if nothing parseable."""
    candidates: List[str] = []
    for m in re.finditer(
        r"```(?:tool_code|python|tool_call|json)?\s*(.*?)```", text, re.DOTALL
    ):
        candidates.append(m.group(1))
    candidates.append(text)  # fallback: the raw text itself

    calls: List[Dict[str, Any]] = []
    seen: set = set()
    for code in candidates:
        for node in _iter_default_api_calls(code):
            tool_name = node.func.attr  # type: ignore[attr-defined]
            args: Dict[str, Any] = {}
            ok = True
            for kw in node.keywords:
                if kw.arg is None:  # **kwargs splat — can't recover
                    ok = False
                    break
                try:
                    args[kw.arg] = ast.literal_eval(kw.value)
                except Exception:
                    ok = False
                    break
            if not ok:
                continue
            sig = (tool_name, tuple(sorted(args)))
            if sig in seen:
                continue
            seen.add(sig)
            calls.append(
                {
                    "name": tool_name,
                    "args": args,
                    "id": f"call_{uuid.uuid4().hex[:24]}",
                    "type": "tool_call",
                }
            )
    return calls


def _maybe_fix(msg: AIMessage) -> Optional[AIMessage]:
    """Return a rewritten AIMessage with recovered tool_calls, or None."""
    if getattr(msg, "tool_calls", None):
        return None  # already structured
    text = _text_of(msg.content)
    if not text or not _LEAK_RE.search(text):
        return None
    calls = extract_leaked_calls(text)
    if not calls:
        return None
    log.info("[tool_recovery] recovered %d leaked tool call(s)", len(calls))
    return AIMessage(
        content="",
        tool_calls=calls,
        id=getattr(msg, "id", None),
        additional_kwargs=getattr(msg, "additional_kwargs", {}) or {},
    )


class LeakedToolCallMiddleware(AgentMiddleware):
    """Convert text-leaked tool calls (Gemini ``default_api`` format) into
    real structured tool_calls so the agent executes them."""

    def _recover(self, response):
        # handler() returns a ModelResponse (.result: list[BaseMessage]) or,
        # on some paths, a bare AIMessage. Handle both; also unwrap the
        # ExtendedModelResponse variant just in case.
        if isinstance(response, AIMessage):
            fixed = _maybe_fix(response)
            return fixed if fixed is not None else response
        inner = getattr(response, "model_response", None)
        target = inner if inner is not None else response
        result = getattr(target, "result", None)
        if isinstance(result, list):
            for i, m in enumerate(result):
                if isinstance(m, AIMessage):
                    fixed = _maybe_fix(m)
                    if fixed is not None:
                        result[i] = fixed
        return response

    def wrap_model_call(self, request, handler):  # type: ignore[override]
        response = handler(request)
        try:
            return self._recover(response)
        except Exception as e:  # never break the turn over recovery
            log.warning("[tool_recovery] failed: %r", e)
            return response

    async def awrap_model_call(self, request, handler):  # type: ignore[override]
        response = await handler(request)
        try:
            return self._recover(response)
        except Exception as e:
            log.warning("[tool_recovery] failed: %r", e)
            return response
