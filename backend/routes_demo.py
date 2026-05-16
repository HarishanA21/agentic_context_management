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
import os
import uuid
from time import perf_counter
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    # PR #3 — Tab selector for the Strategy Demo page.
    #   None / "current_methods"     → 2-column current behaviour
    #   "visual_compression"         → 4-column bench:
    #                                   1. Normal Tool Call
    #                                   2. Normal Tool Call + Image
    #                                   3. Code Mode
    #                                   4. Code Mode + Image
    tab: Optional[str] = None


def _auth(request: Request) -> str:
    """Validate Bearer JWT, return user_id. Lazy-imports the verifier
    to avoid a circular import with api.py."""
    from api import get_current_user  # lazy

    return get_current_user(request.headers.get("authorization"))


def _build_demo_agent(
    strategy: str,
    real_tools: list,
    model_name: str,
    *,
    compress: Optional[Dict[str, Any]] = None,
    on_raw: Optional[Callable[[str, str], None]] = None,
):
    """Construct a throwaway agent. Fresh InMemorySaver per call so two
    parallel runs (one per strategy) can't see each other's state.

    PR #3 additions:
      * ``compress``: when set, wraps the agent's tool list with
        ``wrap_tools_with_compression`` so tool returns flow through
        the visual-compression pipeline before reaching the model.
        Keys: ``{mode, threshold_tokens, only_tools, exclude_tools}``.
      * ``on_raw``: callback fired with each tool's *uncompressed*
        text so the demo can collect ground truth for the judge step.
        No-op when compression isn't enabled (the agent already sees
        the raw text directly).
    """
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

    if compress is not None:
        from visual_tool.wrap_tools import wrap_tools_with_compression  # lazy

        agent_tools = wrap_tools_with_compression(
            agent_tools,
            mode=str(compress.get("mode") or "auxiliary"),
            threshold_tokens=int(compress.get("threshold_tokens") or 500),
            only_tools=compress.get("only_tools"),
            exclude_tools=compress.get("exclude_tools"),
            on_raw=on_raw,
        )

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
    *,
    compress: Optional[Dict[str, Any]] = None,
    label: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one strategy end-to-end and collect metrics.

    PR #3 additions:
      * ``compress`` — when set, the tool list is wrapped with
        ``wrap_tools_with_compression`` so each tool's return is
        rendered as a PNG + REFERENCES block before the agent sees it.
        Keys: ``{mode, threshold_tokens, only_tools, exclude_tools}``.
      * ``label`` — display name (e.g. "Normal Tool Call + Image")
        used when the caller is rendering N>2 columns. Defaults to the
        strategy string for back-compat.
    """
    from api import _unwrap_exc  # lazy

    # Per-run accumulator for the un-compressed tool outputs. Always
    # collected so the judge step (in the 4-column tab) has ground truth
    # regardless of which column it is scoring.
    raw_tool_outputs: List[Tuple[str, str]] = []

    def _collect_raw(tool_name: str, raw: str) -> None:
        raw_tool_outputs.append((tool_name, raw))

    try:
        agent = _build_demo_agent(
            strategy,
            real_tools,
            model_name,
            compress=compress,
            on_raw=_collect_raw,
        )
    except Exception as e:
        return {
            "strategy": strategy,
            "label": label or strategy,
            "ok": False,
            "error": f"build failed: {type(e).__name__}: {str(e)[:200]}",
            "metrics": {"latency_ms": 0},
            "tool_events": [],
            "reply": "",
            "raw_tool_outputs": [],
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
            "label": label or strategy,
            "ok": False,
            "error": f"{type(inner).__name__}: {str(inner)[:300]}",
            "metrics": {"latency_ms": int((perf_counter() - t0) * 1000)},
            "tool_events": [],
            "reply": "",
            "raw_tool_outputs": raw_tool_outputs,
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
        "label": label or strategy,
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
        # Plain-text record of every tool call's *uncompressed* output.
        # The judge step (4-column tab) reads this so its score reflects
        # what was actually available, not what the model saw post-image.
        "raw_tool_outputs": raw_tool_outputs,
    }


# ─── 4-column "visual compression bench" orchestration ────────────────


# Tab 2 of the Strategy Demo. Same prompt × 4 column configs, run in
# parallel, then one GPT-4o judge call to score each reply against the
# canonical raw tool outputs (Column 1's baseline = ground truth).
_VISUAL_COLUMNS: List[Dict[str, Any]] = [
    {
        "label": "Normal Tool Call",
        "strategy": "tool_calling",
        "compress": None,
    },
    {
        "label": "Normal Tool Call + Image",
        "strategy": "tool_calling",
        "compress": {
            # Tools aren't predefined here — let the auxiliary LLM
            # generate a template per tool name (cached).
            "mode": "auxiliary",
            "threshold_tokens": 500,
            # `exclude_tools`: keep tiny / curated outputs out — image
            # overhead outweighs savings on them.
            "exclude_tools": ["calculator", "get_weather"],
        },
    },
    {
        "label": "Code Mode",
        "strategy": "ts_code_mode",
        "compress": None,
    },
    {
        "label": "Code Mode + Image",
        "strategy": "ts_code_mode",
        "compress": {
            # Code Mode has exactly two top-level tools — both have
            # hand-written templates in visual_tool.templates.
            "mode": "templated",
            "threshold_tokens": 500,
            "only_tools": ["describe_tools", "execute_typescript"],
        },
    },
]


# Pin the judge model so accuracy scores stay stable across runs.
# Override with `DEMO_JUDGE_MODEL` env var if you want to test another.
_JUDGE_MODEL_DEFAULT = "openai/gpt-4o"

_JUDGE_SYSTEM = (
    "You are a strict factual-grounding judge. Given the user's prompt, "
    "the raw tool outputs the agent had access to, and the agent's final "
    "reply, score the reply 0-100 on whether its claims, names, URLs, and "
    "citations are actually supported by the raw tool outputs. Penalise "
    "fabricated facts, mis-spelled author names, invented URLs, and "
    "claims that contradict the source. Reward concise faithful answers."
)

_JUDGE_PROMPT_TEMPLATE = """\
USER PROMPT:
<<<
{prompt}
>>>

GROUND-TRUTH RAW TOOL OUTPUTS (the agent had this data available — the
agent itself may have seen text or an image, but you always score against
the raw text):
<<<
{raw}
>>>

AGENT'S FINAL REPLY:
<<<
{reply}
>>>

Score the reply 0-100 across these criteria, weighted equally:
  1. Factual Precision  — are claims actually in the raw outputs?
  2. Reference Verification — are URLs / citations real and unmodified?
  3. Source Coverage — does it use the relevant tool data?
  4. Hallucination Severity (inverted) — how much is fabricated?

Reply with ONLY a JSON object:
  {{"score": <int 0-100>, "reasoning": "<one sentence>"}}

Do not include code fences or any other text.
"""


def _flatten_raw_outputs(items: List[Tuple[str, str]], *, cap: int = 12_000) -> str:
    """Concatenate the (tool_name, text) pairs into a single block the
    judge can read, with a hard byte cap so a runaway tool doesn't blow
    out the judge's context."""
    parts: List[str] = []
    used = 0
    for name, text in items:
        header = f"\n--- tool: {name} ---\n"
        chunk = header + (text or "")
        if used + len(chunk) > cap:
            remaining = cap - used
            if remaining > len(header) + 20:
                parts.append(header + (text or "")[: remaining - len(header) - 4] + " […]")
            break
        parts.append(chunk)
        used += len(chunk)
    return "".join(parts) or "(no tool outputs recorded)"


async def _run_judge(
    prompt: str,
    reply: str,
    raw_outputs: List[Tuple[str, str]],
) -> Dict[str, Any]:
    """One GPT-4o call. Returns ``{"score": int, "reasoning": str}``.
    Always returns a result block — never raises into the demo path."""
    import json
    from langchain_core.messages import HumanMessage, SystemMessage
    from api import _build_model  # lazy

    if not (reply or "").strip():
        return {"score": 0, "reasoning": "agent produced no reply"}

    judge_slug = (
        os.getenv("DEMO_JUDGE_MODEL") or _JUDGE_MODEL_DEFAULT
    ).strip() or _JUDGE_MODEL_DEFAULT
    try:
        model = _build_model(judge_slug)
    except Exception as e:
        return {"score": -1, "reasoning": f"judge build failed: {e}"}

    user = _JUDGE_PROMPT_TEMPLATE.format(
        prompt=(prompt or "")[:2_000],
        raw=_flatten_raw_outputs(raw_outputs),
        reply=(reply or "")[:6_000],
    )
    try:
        resp = await model.ainvoke(
            [SystemMessage(content=_JUDGE_SYSTEM), HumanMessage(content=user)]
        )
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
    except Exception as e:
        return {"score": -1, "reasoning": f"judge call failed: {type(e).__name__}: {e}"}

    # Tolerant JSON parse — model may add prose despite instructions.
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        parsed = json.loads(text)
        score = int(parsed.get("score", -1))
        reasoning = str(parsed.get("reasoning", ""))[:300]
        return {"score": max(-1, min(100, score)), "reasoning": reasoning}
    except Exception as e:
        return {"score": -1, "reasoning": f"judge parse failed: {e}"}


async def _run_visual_compression_tab(
    body: DemoCompareRequest,
    user_id: str,
    real_tools: list,
    model_name: str,
) -> Dict[str, Any]:
    """4-column run for Tab 2. Runs every column in parallel, then fires
    one judge call per column once the runs settle."""
    rid = uuid.uuid4().hex[:8]
    tasks = [
        _run_one(
            col["strategy"],
            body.prompt.strip(),
            model_name,
            user_id,
            real_tools,
            thread_id=f"demo:{user_id}:{rid}:v{i}",
            compress=col.get("compress"),
            label=col["label"],
        )
        for i, col in enumerate(_VISUAL_COLUMNS)
    ]
    raw = await asyncio.gather(*tasks, return_exceptions=True)

    results: List[Dict[str, Any]] = []
    for col, item in zip(_VISUAL_COLUMNS, raw):
        if isinstance(item, BaseException):
            import traceback

            print(
                f"[/demo/compare] visual col {col['label']!r} escaped: "
                f"{type(item).__name__}: {item}",
                flush=True,
            )
            traceback.print_exc()
            results.append(
                {
                    "strategy": col["strategy"],
                    "label": col["label"],
                    "ok": False,
                    "error": f"{type(item).__name__}: {str(item)[:300]}",
                    "metrics": {"latency_ms": 0},
                    "tool_events": [],
                    "reply": "",
                    "raw_tool_outputs": [],
                }
            )
        else:
            results.append(item)

    # Use Column 1 (baseline tool_calling) raw outputs as ground truth
    # for every judge call — the paper's pattern. Falls back to each
    # column's own raw outputs if the baseline had none.
    baseline_raw = results[0].get("raw_tool_outputs") if results else []

    judge_tasks = [
        _run_judge(
            body.prompt.strip(),
            r.get("reply", ""),
            baseline_raw or r.get("raw_tool_outputs", []),
        )
        for r in results
    ]
    judge_results = await asyncio.gather(*judge_tasks, return_exceptions=True)
    for r, j in zip(results, judge_results):
        if isinstance(j, BaseException):
            r["accuracy"] = {"score": -1, "reasoning": f"{type(j).__name__}"}
        else:
            r["accuracy"] = j
        # Strip raw outputs from the wire payload — they were only for
        # the judge step. Saves a chunk of response bytes.
        r.pop("raw_tool_outputs", None)

    # Compression ratios: Col 2 vs Col 1, Col 4 vs Col 3 (per the plan).
    def _input_tokens(idx: int) -> int:
        try:
            return int(results[idx]["metrics"].get("input_tokens") or 0)
        except (KeyError, IndexError, TypeError):
            return 0

    def _ratio(after: int, before: int) -> Optional[float]:
        if not before:
            return None
        saved = max(0, before - after)
        return round(saved / before, 4)

    compression_ratios = {
        "column_2_vs_1": _ratio(_input_tokens(1), _input_tokens(0)),
        "column_4_vs_3": _ratio(_input_tokens(3), _input_tokens(2)),
    }

    return {
        "prompt": body.prompt.strip(),
        "model": model_name,
        "tab": "visual_compression",
        "judge_model": (
            os.getenv("DEMO_JUDGE_MODEL") or _JUDGE_MODEL_DEFAULT
        ).strip()
        or _JUDGE_MODEL_DEFAULT,
        "results": results,
        "compression_ratios": compression_ratios,
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

    # PR #3: dispatch to the 4-column visual-compression orchestrator
    # when the new tab is selected. Tab 1 (or no tab) falls through to
    # the original 2-column path below — fully back-compat.
    if (body.tab or "").strip() == "visual_compression":
        return await _run_visual_compression_tab(
            body, user_id, real_tools, model_name
        )

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
