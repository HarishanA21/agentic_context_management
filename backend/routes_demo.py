"""Side-by-side strategy comparison endpoint.

Powers the Strategy Demo page: one prompt → both `tool_calling` and
`ts_code_mode` run *in parallel* with isolated in-memory checkpointers,
returning latency + token + tool-call metrics + final reply for each.

Why isolated state:
  * We must not write demo turns into the user's real chat history.
  * Each strategy gets a fresh `InMemorySaver` and a one-off thread_id
    so checkpoints don't collide and nothing persists past the
    response.

Why only safe builtins for the demo:
  * The demo is a comparison surface — `write_project_file` would mint
    real S3 garbage and `run_shell` would touch the workspace. The
    point is metrics, not side effects. MCP tools are still included
    because they're typically read-only and a power-user wants to
    demo their stack.

Import discipline: nothing imports from `api` at module load time —
mirrors `routes_mcp.py` to avoid the circular dependency you'd
otherwise hit because `api.py` mounts this router after the FastAPI
app is built.
"""

from __future__ import annotations

import asyncio
import uuid
from time import perf_counter
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel


router = APIRouter()


# Whitelist of built-in tools that are safe to run inside a demo turn.
# Read-only operations only; write_project_file and run_shell stay out.
_DEMO_SAFE_BUILTINS = {
    "calculator",
    "get_weather",
    "list_project_files",
    "read_project_file",
}


class DemoCompareRequest(BaseModel):
    prompt: str
    # Defaults to both strategies. Unknown ids are silently coerced to
    # the global default by _normalise_strategy.
    strategies: Optional[List[str]] = None
    model: Optional[str] = None


def _auth(request: Request) -> str:
    """Validate Bearer JWT, return user_id. Lazy-imports the verifier
    to avoid a circular import with api.py."""
    from api import get_current_user  # lazy

    return get_current_user(request.headers.get("authorization"))


def _build_demo_agent(strategy: str, real_tools: list, model_name: str):
    """Construct a throwaway agent. Fresh InMemorySaver per call so two
    parallel runs (one per strategy) can't see each other's state."""
    from api import SYSTEM_PROMPT, _build_model  # lazy

    saver = InMemorySaver()
    if strategy == "ts_code_mode":
        from Tools.describe_tools_tool import make_describe_tools_tool
        from Tools.execute_typescript_tool import make_execute_typescript_tool
        from ts_code_mode import ts_code_mode_system_prompt

        agent_tools = [
            make_describe_tools_tool(real_tools),
            make_execute_typescript_tool(real_tools),
        ]
        system_prompt = ts_code_mode_system_prompt(real_tools)
    else:
        agent_tools = list(real_tools)
        system_prompt = SYSTEM_PROMPT
    return create_agent(
        model=_build_model(model_name),
        tools=agent_tools,
        system_prompt=system_prompt,
        checkpointer=saver,
    )


async def _run_one(
    strategy: str,
    prompt: str,
    model_name: str,
    user_id: str,
    real_tools: list,
    thread_id: str,
) -> Dict[str, Any]:
    """Run one strategy end-to-end and collect metrics."""
    from api import _unwrap_exc  # lazy

    try:
        agent = _build_demo_agent(strategy, real_tools, model_name)
    except Exception as e:
        return {
            "strategy": strategy,
            "ok": False,
            "error": f"build failed: {type(e).__name__}: {str(e)[:200]}",
            "metrics": {"latency_ms": 0},
            "tool_events": [],
            "reply": "",
        }

    config = {
        "configurable": {
            "thread_id": thread_id,
            "user_id": user_id,
            "session_id": f"demo-{strategy}",
            "session_mode": "auto",
        }
    }

    t0 = perf_counter()
    try:
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=prompt)]},
            config=config,
        )
    except BaseException as e:
        # BaseException — covers GraphRecursionError, RateLimit subclasses,
        # anyio ExceptionGroup, and friends that don't subclass Exception.
        # The demo must always render a result block for the column, even
        # on failure, so we swallow + report rather than letting it
        # propagate out of gather().
        import traceback

        inner = _unwrap_exc(e) if isinstance(e, Exception) else e
        print(
            f"[/demo/compare] strategy={strategy} invoke failed: "
            f"{type(inner).__name__}: {inner}",
            flush=True,
        )
        traceback.print_exc()
        return {
            "strategy": strategy,
            "ok": False,
            "error": f"{type(inner).__name__}: {str(inner)[:300]}",
            "metrics": {"latency_ms": int((perf_counter() - t0) * 1000)},
            "tool_events": [],
            "reply": "",
        }
    latency_ms = int((perf_counter() - t0) * 1000)

    messages = result.get("messages") or []
    input_tokens = 0
    output_tokens = 0
    tool_events: list[dict] = []
    final_reply = ""
    for m in messages:
        if isinstance(m, AIMessage):
            um = getattr(m, "usage_metadata", None) or {}
            input_tokens += int(um.get("input_tokens") or 0)
            output_tokens += int(um.get("output_tokens") or 0)
            for tc in m.tool_calls or []:
                tool_events.append(
                    {
                        "name": tc.get("name") or "",
                        "args_keys": list((tc.get("args") or {}).keys()),
                    }
                )
            content = m.content
            if isinstance(content, str) and content.strip():
                final_reply = content
        elif isinstance(m, ToolMessage):
            # Counted via the AIMessage that triggered the call.
            pass

    return {
        "strategy": strategy,
        "ok": True,
        "reply": final_reply,
        "metrics": {
            "latency_ms": latency_ms,
            "tool_calls": len(tool_events),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        "tool_events": tool_events,
    }


@router.post("/demo/compare")
async def demo_compare(request: Request, body: DemoCompareRequest):
    """Run the same prompt under multiple context-management strategies
    concurrently. Returns one result block per strategy with comparable
    metrics. No DB writes, no chat history pollution.
    """
    user_id = _auth(request)
    from api import (  # lazy
        DEFAULT_MODEL,
        _VALID_CONTEXT_STRATEGIES,
        _collect_mcp_tools_async,
        _normalise_strategy,
    )
    from Tools import all_tools

    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt required")

    requested = body.strategies or list(_VALID_CONTEXT_STRATEGIES)
    seen: set[str] = set()
    strategies: list[str] = []
    for s in requested:
        norm = _normalise_strategy(s)
        if norm in seen:
            continue
        seen.add(norm)
        strategies.append(norm)
    if not strategies:
        raise HTTPException(400, "no valid strategies")

    model_name = (body.model or DEFAULT_MODEL).strip() or DEFAULT_MODEL

    try:
        mcp_tools = await _collect_mcp_tools_async(user_id)
    except Exception as e:
        # MCP discovery should never block the demo. Log + continue
        # with just the safe builtins.
        print(f"[/demo/compare] MCP discovery failed: {e!r}", flush=True)
        mcp_tools = []
    safe_builtins = [t for t in all_tools if t.name in _DEMO_SAFE_BUILTINS]
    real_tools = safe_builtins + list(mcp_tools)

    rid = uuid.uuid4().hex[:8]
    tasks = [
        _run_one(
            s,
            prompt,
            model_name,
            user_id,
            real_tools,
            thread_id=f"demo:{user_id}:{rid}:{s}",
        )
        for s in strategies
    ]
    # return_exceptions=True so a fault in one strategy never erases the
    # other's column. Anything that escapes _run_one's BaseException
    # guard becomes a synthetic error block here.
    raw = await asyncio.gather(*tasks, return_exceptions=True)
    results: list[dict] = []
    for s, item in zip(strategies, raw):
        if isinstance(item, BaseException):
            import traceback

            print(
                f"[/demo/compare] strategy={s} escaped guard: "
                f"{type(item).__name__}: {item}",
                flush=True,
            )
            traceback.print_exc()
            results.append(
                {
                    "strategy": s,
                    "ok": False,
                    "error": f"{type(item).__name__}: {str(item)[:300]}",
                    "metrics": {"latency_ms": 0},
                    "tool_events": [],
                    "reply": "",
                }
            )
        else:
            results.append(item)

    return {
        "prompt": prompt,
        "model": model_name,
        "results": results,
    }
