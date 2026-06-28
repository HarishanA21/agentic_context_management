"""Per-request decorator that wraps a tool list with compression.

Used only by the Strategy Demo's "Visual Compression Bench" tab —
the main /chat path is unaffected. For each tool in the input list:

  * If it's excluded (or not in the allow-list), pass through.
  * Otherwise, build a new StructuredTool whose async coroutine
    calls the original tool, then post-processes the return value
    through ``maybe_compress_async``. If compression fires, the
    coroutine returns a *list of content blocks* (text REFERENCES +
    base64 PNG) which LangGraph's tool node automatically packages
    into a multimodal ``ToolMessage`` with the correct ``tool_call_id``.

Why "list of content blocks" instead of a ToolMessage:
  Returning a ToolMessage directly would require us to know the
  ``tool_call_id`` at wrap time, which we don't. Returning the
  ``content`` (the list) lets LangGraph fill in the id during wrap.
"""

from __future__ import annotations

import inspect
import logging
from typing import Callable, Iterable, List, Optional, Set

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool

from .compressor import DEFAULT_THRESHOLD_TOKENS, maybe_compress_async


# Optional hook fired with the *raw* (uncompressed) tool output. Used by
# the Strategy Demo's judge step to score the agent's final reply
# against the ground-truth tool data, regardless of whether the agent
# itself saw text or an image. Signature: ``(tool_name, raw_str) -> None``.
RawOutputCallback = Callable[[str, str], None]


log = logging.getLogger(__name__)


def _norm(names: Optional[Iterable[str]]) -> Optional[Set[str]]:
    if names is None:
        return None
    return {n for n in names if n}


def wrap_tools_with_compression(
    tools: List[BaseTool],
    *,
    mode: str = "auxiliary",
    threshold_tokens: int = DEFAULT_THRESHOLD_TOKENS,
    only_tools: Optional[Iterable[str]] = None,
    exclude_tools: Optional[Iterable[str]] = None,
    on_raw: Optional[RawOutputCallback] = None,
) -> List[BaseTool]:
    """Return a new list of tools where each tool's return value (if
    above ``threshold_tokens``) is rasterised + indexed before reaching
    the agent's message list.

    Args:
        tools: the agent's normal tool list (built-ins + MCP tools).
        mode: ``"auxiliary"`` (Column 2 default — Gemini-Flash-generates
            a Python format function on first sighting, then caches)
            or ``"templated"`` (Column 4 default — uses hand-written
            templates from ``visual_tool.templates``; falls back to
            auxiliary for any tool that has no template).
        threshold_tokens: below this, the tool's return passes through
            unchanged (image overhead isn't worth it for small outputs).
        only_tools: when set, ONLY wrap tools whose name is in this set.
            Lets Column 4 surgically wrap just the two Code Mode entry
            points (``describe_tools``, ``execute_typescript``).
        exclude_tools: tools whose name is in this set are never
            wrapped. Use for tools whose output is small / curated /
            already structured (e.g. ``calculator``).
    """
    only = _norm(only_tools)
    exclude = _norm(exclude_tools) or set()
    wrapped: List[BaseTool] = []
    for tool in tools:
        name = tool.name
        if name in exclude:
            wrapped.append(tool)
            continue
        if only is not None and name not in only:
            wrapped.append(tool)
            continue
        wrapped.append(
            _wrap_one(
                tool,
                mode=mode,
                threshold_tokens=threshold_tokens,
                on_raw=on_raw,
            )
        )
    return wrapped


# ─── per-tool wrapper construction ─────────────────────────────────────


def _wrap_one(
    original: BaseTool,
    *,
    mode: str,
    threshold_tokens: int,
    on_raw: Optional[RawOutputCallback] = None,
) -> BaseTool:
    """Build a new StructuredTool whose coroutine calls ``original``
    and then runs ``maybe_compress_async`` on the result."""

    tool_name = original.name
    tool_description = original.description
    args_schema = original.args_schema

    async def _wrapped_coro(config: RunnableConfig, **kwargs) -> object:
        # 1. Call the original tool. We forward `config` explicitly so
        #    tools that need user_id / session_id (every file tool) see
        #    the same RunnableConfig the wrapper itself was invoked with.
        try:
            raw = await original.ainvoke(kwargs, config=config)
        except Exception as e:
            # Surface as an error string — same shape an unwrapped tool
            # would produce when it raises. Lets the agent recover.
            log.warning(
                "[visual_tool] wrapped tool %r raised %r — passing through error",
                tool_name, e,
            )
            return f"Error: {type(e).__name__}: {e}"

        # 2. Coerce to text for the compressor.
        if isinstance(raw, str):
            text = raw
        elif raw is None:
            text = ""
        else:
            try:
                text = str(raw)
            except Exception:
                text = repr(raw)

        # 2a. Notify the caller of the *raw* (un-compressed) output if
        #     they registered a hook. The Strategy Demo uses this to
        #     accumulate ground-truth tool data that the judge step
        #     later scores against — same text for every column, so
        #     the judge always sees what was really there even when
        #     the agent saw an image.
        if on_raw is not None:
            try:
                on_raw(tool_name, text)
            except Exception as e:  # never let the hook break the run
                log.warning(
                    "[visual_tool] on_raw callback raised %r — ignored", e
                )

        # 3. Maybe compress.
        try:
            result = await maybe_compress_async(
                tool_name=tool_name,
                output=text,
                mode=mode,
                threshold_tokens=threshold_tokens,
                tool_call_id="",  # LangGraph fills the real id when wrapping
            )
        except Exception as e:
            log.warning(
                "[visual_tool] compression failed for %r: %r — using raw",
                tool_name, e,
            )
            return text

        # 4. Hand back what LangGraph can wrap.
        #    - If it's a ToolMessage, return its `.content` (the list of
        #      multimodal blocks). LangGraph builds a fresh ToolMessage
        #      with the right tool_call_id and that content.
        #    - If it's a raw string (below threshold), return as-is.
        if isinstance(result, ToolMessage):
            return result.content
        return result

    # Set a useful __signature__ so StructuredTool.from_function can
    # introspect cleanly. We declare the same param names the original
    # args_schema exposes, plus a `config: RunnableConfig` so LangChain
    # auto-injects the runnable config at call time.
    if args_schema is not None and hasattr(args_schema, "model_fields"):
        params = [
            inspect.Parameter(
                "config",
                kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=RunnableConfig,
            )
        ]
        for fname, finfo in args_schema.model_fields.items():
            params.append(
                inspect.Parameter(
                    fname,
                    kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=(
                        inspect.Parameter.empty
                        if finfo.is_required()
                        else (finfo.default if finfo.default is not None else None)
                    ),
                    annotation=finfo.annotation,
                )
            )
        try:
            _wrapped_coro.__signature__ = inspect.Signature(parameters=params)  # type: ignore[attr-defined]
        except (TypeError, ValueError):
            pass

    return StructuredTool.from_function(
        coroutine=_wrapped_coro,
        name=tool_name,
        description=tool_description,
        args_schema=args_schema,
    )
