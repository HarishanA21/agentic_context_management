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
from fastapi.responses import StreamingResponse
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
    # Visual-compression tab batching: the client runs the 10 columns in
    # small batches (2 at a time) to avoid provider rate limits, so it asks
    # for a subset of column indices per request. None ⇒ run all columns.
    columns: Optional[List[int]] = None
    # When a batch doesn't include column 0 (the ground-truth baseline), the
    # client passes the baseline's reply (captured from the first batch) so
    # this batch's columns can still be judged against it.
    baseline_reply: Optional[str] = None
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
    image_recall: Optional[Dict[str, Any]] = None,
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

    # Optional image-recall technique (caching and/or within-loop image
    # eviction). When set, attach the provider-agnostic middleware so the
    # demo's single-turn tool loop exercises the selected method.
    middleware = []
    if image_recall and (image_recall.get("mode") or "off") != "off":
        from cache_layout import ImageRecallMiddleware  # lazy

        middleware = [
            ImageRecallMiddleware(
                mode=str(image_recall.get("mode") or "off"),
                keep_recent_images=int(image_recall.get("keep_recent_images") or 3),
                ttl=str(image_recall.get("cache_ttl") or "5m"),
            )
        ]

    return create_agent(
        model=_build_model(model_name),
        tools=agent_tools,
        system_prompt=system_prompt,
        checkpointer=saver,
        middleware=middleware,
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
    image_recall: Optional[Dict[str, Any]] = None,
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
            image_recall=image_recall,
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

    from cache_layout import read_cache_tokens  # lazy

    messages = result.get("messages") or []
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0   # image-recall caching: tokens served from cache
    cache_write_tokens = 0  # tokens written into the cache (first sighting)
    image_blocks = 0       # PR #5 — count multimodal image parts the agent saw
    image_messages = 0     # PR #5 — distinct ToolMessages that carried at least one image
    tool_events: list[dict] = []
    final_reply = ""
    for m in messages:
        # PR #5: count image content blocks across all messages. The
        # provider's input_tokens already includes their billed cost,
        # so we don't double-count tokens — we just surface the count
        # so the user can verify the compressed columns sent images.
        content = getattr(m, "content", None)
        if isinstance(content, list):
            n_images = sum(
                1
                for b in content
                if isinstance(b, dict)
                and b.get("type") in {"image_url", "image"}
            )
            if n_images:
                image_blocks += n_images
                image_messages += 1
        if isinstance(m, AIMessage):
            um = getattr(m, "usage_metadata", None) or {}
            input_tokens += int(um.get("input_tokens") or 0)
            output_tokens += int(um.get("output_tokens") or 0)
            ct = read_cache_tokens(m)
            cache_read_tokens += ct["cache_read"]
            cache_write_tokens += ct["cache_write"]
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
            # PR #5 — provability that image-bearing tool messages
            # actually reached the model. Provider's input_tokens
            # already includes their billed cost — we don't double-count.
            "image_blocks": image_blocks,
            "image_messages": image_messages,
            # Image-recall caching: tokens served from / written to the
            # provider prompt cache across this column's model calls. Zero
            # for non-cache columns (and for providers that don't report it).
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "cache_hit_ratio": (
                round(cache_read_tokens / input_tokens, 4)
                if input_tokens
                else None
            ),
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
    # ── image-recall columns (5-10). Each applies ONE technique on top of an
    #    existing image method — NOT a mix across columns:
    #      Col 5-7  = Col 2 ("Normal Tool Call + Image", raw-image) + cache / evict / both
    #      Col 8-10 = Col 4 ("Code Mode + Image", TS+image)        + cache / evict / both
    #    They reuse the exact compress config of Col 2 / Col 4 so the only
    #    variable is the image_recall mode. Columns 1-4 carry no image_recall.
    {
        # Col 5 = Col 2 + caching only
        "label": "Col 2 + Caching",
        "strategy": "tool_calling",
        "compress": {"mode": "auxiliary", "threshold_tokens": 500, "exclude_tools": ["calculator", "get_weather"]},
        "image_recall": {"mode": "cache", "cache_ttl": "5m"},
    },
    {
        # Col 6 = Col 2 + evicting only
        "label": "Col 2 + Evicting (K=3)",
        "strategy": "tool_calling",
        "compress": {"mode": "auxiliary", "threshold_tokens": 500, "exclude_tools": ["calculator", "get_weather"]},
        "image_recall": {"mode": "evict", "keep_recent_images": 3},
    },
    {
        # Col 7 = Col 2 + caching + evicting
        "label": "Col 2 + Caching + Evicting (K=3)",
        "strategy": "tool_calling",
        "compress": {"mode": "auxiliary", "threshold_tokens": 500, "exclude_tools": ["calculator", "get_weather"]},
        "image_recall": {"mode": "cache_evict", "keep_recent_images": 3, "cache_ttl": "5m"},
    },
    {
        # Col 8 = Col 4 + caching only
        "label": "Col 4 + Caching",
        "strategy": "ts_code_mode",
        "compress": {"mode": "templated", "threshold_tokens": 500, "only_tools": ["describe_tools", "execute_typescript"]},
        "image_recall": {"mode": "cache", "cache_ttl": "5m"},
    },
    {
        # Col 9 = Col 4 + evicting only
        "label": "Col 4 + Evicting (K=3)",
        "strategy": "ts_code_mode",
        "compress": {"mode": "templated", "threshold_tokens": 500, "only_tools": ["describe_tools", "execute_typescript"]},
        "image_recall": {"mode": "evict", "keep_recent_images": 3},
    },
    {
        # Col 10 = Col 4 + caching + evicting
        "label": "Col 4 + Caching + Evicting (K=3)",
        "strategy": "ts_code_mode",
        "compress": {"mode": "templated", "threshold_tokens": 500, "only_tools": ["describe_tools", "execute_typescript"]},
        "image_recall": {"mode": "cache_evict", "keep_recent_images": 3, "cache_ttl": "5m"},
    },
]


# Pin the judge model so accuracy scores stay stable across runs.
# Override with `DEMO_JUDGE_MODEL` env var if you want to test another.
_JUDGE_MODEL_DEFAULT = "google/gemini-3.1-flash-lite"

_JUDGE_SYSTEM = """\
You are an output-comparison judge. Your job is to measure how faithfully a candidate
agent reply preserves the meaning and key values of a baseline reply.

The baseline is the ground truth. The candidate received the same underlying data
in a different encoding (compressed image, code-mode text, or image of code output).
Your role is to reason carefully about each difference you find — not to pattern-match
on surface text — and then score based on your conclusions.\
"""

_JUDGE_PROMPT_TEMPLATE = """\
ORIGINAL USER PROMPT:
<<<
{prompt}
>>>

BASELINE REPLY (ground truth — treat this as 100% correct):
<<<
{baseline_reply}
>>>

CANDIDATE REPLY (the method being evaluated):
<<<
{candidate_reply}
>>>

════════════════════════════════════════
STEP 1 — ANALYSE EVERY DIFFERENCE FIRST
════════════════════════════════════════

Before scoring anything, go through every difference you notice and answer these
questions for each one. Write your answers in the "thinking" field.

For each differing NUMBER:
  Q1. If I round the baseline value to the same number of decimal places the
      candidate used, do they match?
  Q2. Are any significant digits actually wrong — i.e. would the difference matter
      to someone reading the final answer?
  Q3. Based on Q1 and Q2: is this a precision/rounding difference, or a real error?

For each differing PHRASE, LABEL, or TIME EXPRESSION:
  Q4. Does each version describe the same quantity or event, just worded differently?
  Q5. Would a reader of the candidate reach a different factual conclusion than a
      reader of the baseline?
  Q6. Based on Q4 and Q5: is this a presentation difference, or a meaning difference?

For each item the candidate contains that the baseline does NOT:
  Q7. Can this item be derived from, or is it equivalent to, something already in
      the baseline (e.g. a reformatted label, a rounded number, a synonym)?
  Q8. Does it assert a new fact, value, or claim that has no basis in the baseline?
  Q9. Based on Q7 and Q8: is this a formatting choice, or a fabricated addition?

Only after answering those questions proceed to Step 2.

════════════════════════════
STEP 2 — SCORE FOUR CRITERIA
════════════════════════════

Use ONLY the conclusions from Step 1 to justify each score. Do not re-examine the
raw text — score based on what your analysis determined.

  C1. Answer Correctness (1-25)
      Did the candidate's final answer or conclusion match the baseline's?
      Score on meaning, not wording. Use your Step 1 findings on whether differences
      were real errors or presentation choices.
      25 = same meaning; 1 = completely wrong or absent conclusion.

  C2. Value Fidelity (1-25)  [PRIMARY — detects encoding/OCR corruption]
      Were key values faithfully preserved?
      Only count items where Step 1 determined there is a REAL error (Q2=yes or Q3=real error).
      Items where Q3 = "rounding/precision difference" must NOT appear in mutated_values.
      25 = no real corruptions found; 1 = most values are genuinely wrong.

  C3. Completeness (1-25)
      Did the candidate address every sub-task the baseline addressed?
      Count distinct questions or steps in the baseline, check whether each was
      answered in the candidate. Presentation order does not matter.
      25 = all sub-tasks covered; 1 = most sub-tasks missing.

  C4. Hallucination (1-25)  [INVERTED — 25 = no hallucination]
      Did the candidate fabricate anything?
      Only count items where Step 1 determined Q8=yes (new fact with no basis in baseline).
      Items where Q9 = "formatting choice" must NOT appear in hallucinated_claims.
      25 = no fabrications found; 1 = heavily fabricated content.

════════════════
OUTPUT (JSON only — no prose, no code fences)
════════════════

{{
  "thinking": "<your Step 1 Q1-Q9 analysis for every non-trivial difference>",
  "c1_answer_correctness": {{
    "score": <int 1-25>,
    "baseline_answer": "<final answer from baseline>",
    "candidate_answer": "<final answer from candidate>",
    "match": <true|false>
  }},
  "c2_value_fidelity": {{
    "score": <int 1-25>,
    "exact_values": ["<value correctly reproduced>"],
    "mutated_values": ["<only items where Q3=real error: baseline X → candidate Y>"]
  }},
  "c3_completeness": {{
    "score": <int 1-25>,
    "covered": ["..."],
    "missing": ["..."]
  }},
  "c4_hallucination": {{
    "score": <int 1-25>,
    "hallucinated_claims": ["<only items where Q8=yes and Q9=fabricated addition>"]
  }},
  "total_score": <int 0-100>,
  "summary": "<one sentence on the most significant real difference found, or 'no substantive differences' if none>"
}}

Compute total_score as: c1 + c2 + c3 + c4  (they already sum to 100).
"""


async def _run_judge(
    prompt: str,
    baseline_reply: str,
    candidate_reply: str,
) -> Dict[str, Any]:
    """One Qwen3-Max call. Compares candidate_reply against baseline_reply.
    Returns {"score": int, "reasoning": str, "criteria": {...}}.
    Always returns a result block — never raises into the demo path."""
    import json
    from langchain_core.messages import HumanMessage, SystemMessage
    from api import _build_model  # lazy

    if not (candidate_reply or "").strip():
        return {"score": 0, "reasoning": "agent produced no reply", "criteria": {}}

    judge_slug = (
        os.getenv("DEMO_JUDGE_MODEL") or _JUDGE_MODEL_DEFAULT
    ).strip() or _JUDGE_MODEL_DEFAULT
    try:
        model = _build_model(judge_slug).bind(temperature=0.0)
    except Exception as e:
        return {"score": -1, "reasoning": f"judge build failed: {e}", "criteria": {}}

    user = _JUDGE_PROMPT_TEMPLATE.format(
        prompt=(prompt or "")[:2_000],
        baseline_reply=(baseline_reply or "")[:6_000],
        candidate_reply=(candidate_reply or "")[:6_000],
    )
    try:
        resp = await model.ainvoke(
            [SystemMessage(content=_JUDGE_SYSTEM), HumanMessage(content=user)]
        )
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
    except Exception as e:
        return {"score": -1, "reasoning": f"judge call failed: {type(e).__name__}: {e}", "criteria": {}}

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
        score = int(parsed.get("total_score", -1))
        summary = str(parsed.get("summary", ""))[:300]
        criteria = {
            k: parsed[k]
            for k in ("c1_answer_correctness", "c2_value_fidelity", "c3_completeness", "c4_hallucination")
            if k in parsed
        }
        return {"score": max(-1, min(100, score)), "reasoning": summary, "criteria": criteria}
    except Exception as e:
        return {"score": -1, "reasoning": f"judge parse failed: {e}", "criteria": {}}


async def _run_visual_compression_tab(
    body: DemoCompareRequest,
    user_id: str,
    real_tools: list,
    model_name: str,
) -> Dict[str, Any]:
    """Run a *subset* of the 10 visual-bench columns (Tab 2).

    The client drives batching: it asks for a few column indices at a time
    (2, to dodge provider rate limits) via ``body.columns``, shows each
    batch's results, then requests the next. We run only the requested
    columns in parallel, judge them against the baseline reply (column 0's
    output — included in the first batch, then echoed back by the client on
    later batches), and tag every result with its column ``index`` so the UI
    can slot it into the right position. ``columns=None`` runs all columns.
    """
    n_cols = len(_VISUAL_COLUMNS)
    # Resolve + sanitise the requested indices (dedup, in-range, sorted).
    if body.columns is None:
        indices = list(range(n_cols))
    else:
        indices = sorted({i for i in body.columns if isinstance(i, int) and 0 <= i < n_cols})
    if not indices:
        raise HTTPException(400, "no valid column indices")

    rid = uuid.uuid4().hex[:8]
    tasks = [
        _run_one(
            _VISUAL_COLUMNS[i]["strategy"],
            body.prompt.strip(),
            model_name,
            user_id,
            real_tools,
            thread_id=f"demo:{user_id}:{rid}:v{i}",
            compress=_VISUAL_COLUMNS[i].get("compress"),
            image_recall=_VISUAL_COLUMNS[i].get("image_recall"),
            label=_VISUAL_COLUMNS[i]["label"],
        )
        for i in indices
    ]
    raw = await asyncio.gather(*tasks, return_exceptions=True)

    results: List[Dict[str, Any]] = []
    for i, item in zip(indices, raw):
        col = _VISUAL_COLUMNS[i]
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
                    "index": i,
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
            item["index"] = i
            results.append(item)

    by_index = {r["index"]: r for r in results}

    # Baseline reply (column 0 = raw text, ground truth). Prefer column 0's
    # reply when this batch ran it; otherwise use the one the client passed.
    if 0 in by_index:
        baseline_reply = by_index[0].get("reply") or ""
        by_index[0]["accuracy"] = {
            "score": 100,
            "reasoning": "baseline — raw text, ground truth",
            "criteria": {},
        }
    else:
        baseline_reply = (body.baseline_reply or "")

    # Judge every non-baseline column in this batch against the baseline reply.
    to_judge = [r for r in results if r["index"] != 0]
    judge_results = await asyncio.gather(
        *[
            _run_judge(body.prompt.strip(), baseline_reply, r.get("reply", ""))
            for r in to_judge
        ],
        return_exceptions=True,
    )
    for r, j in zip(to_judge, judge_results):
        if isinstance(j, BaseException):
            r["accuracy"] = {"score": -1, "reasoning": f"{type(j).__name__}", "criteria": {}}
        else:
            r["accuracy"] = j
    for r in results:
        r["context_window"] = r.pop("raw_tool_outputs", [])

    return {
        "prompt": body.prompt.strip(),
        "model": model_name,
        "tab": "visual_compression",
        "judge_model": (
            os.getenv("DEMO_JUDGE_MODEL") or _JUDGE_MODEL_DEFAULT
        ).strip()
        or _JUDGE_MODEL_DEFAULT,
        "results": results,
        # Echo the baseline reply so the client can carry it into later
        # batches (which won't re-run column 0). Compression ratios are
        # computed client-side from the accumulated columns' input tokens.
        "baseline_reply": baseline_reply,
    }


# ─── streaming visual bench (SSE, 4 columns in parallel) ───────────────

# How many columns run concurrently. 4 keeps the UI lively while staying
# well under provider rate limits (the old client batched 2 at a time).
_VISUAL_STREAM_CONCURRENCY = 4


async def _visual_stream_events(
    body: DemoCompareRequest,
    user_id: str,
    real_tools: list,
    model_name: str,
):
    """Async generator of SSE frames for the streaming visual bench.

    Runs all 10 columns with a concurrency cap of ``_VISUAL_STREAM_CONCURRENCY``
    and emits one ``column`` frame the moment a column's run *and* its judge
    call have both finished — so the UI fills in live, in completion order.
    Column 0 (raw text) is the ground-truth baseline every other column is
    judged against; non-baseline columns wait for it before judging.
    """
    import json as _json

    judge_model = (
        os.getenv("DEMO_JUDGE_MODEL") or _JUDGE_MODEL_DEFAULT
    ).strip() or _JUDGE_MODEL_DEFAULT
    prompt = (body.prompt or "").strip()
    indices = list(range(len(_VISUAL_COLUMNS)))
    rid = uuid.uuid4().hex[:8]

    def _sse(obj: Dict[str, Any]) -> bytes:
        return f"data: {_json.dumps(obj)}\n\n".encode("utf-8")

    yield _sse(
        {
            "type": "meta",
            "prompt": prompt,
            "model": model_name,
            "judge_model": judge_model,
            "total": len(indices),
            "concurrency": _VISUAL_STREAM_CONCURRENCY,
        }
    )

    sem = asyncio.Semaphore(_VISUAL_STREAM_CONCURRENCY)
    baseline_reply = {"v": ""}
    baseline_ready = asyncio.Event()

    async def run_col(i: int) -> Dict[str, Any]:
        col = _VISUAL_COLUMNS[i]
        async with sem:
            res = await _run_one(
                col["strategy"],
                prompt,
                model_name,
                user_id,
                real_tools,
                thread_id=f"demo:{user_id}:{rid}:v{i}",
                compress=col.get("compress"),
                image_recall=col.get("image_recall"),
                label=col["label"],
            )
        res["index"] = i
        if i == 0:
            baseline_reply["v"] = res.get("reply") or ""
            baseline_ready.set()
        return res

    tasks = [asyncio.create_task(run_col(i)) for i in indices]
    if 0 not in indices:  # defensive — col 0 is always present for a full run
        baseline_ready.set()

    try:
        for fut in asyncio.as_completed(tasks):
            res = await fut
            i = res["index"]
            if i == 0:
                res["accuracy"] = {
                    "score": 100,
                    "reasoning": "baseline — raw text, ground truth",
                    "criteria": {},
                }
            else:
                await baseline_ready.wait()
                try:
                    res["accuracy"] = await _run_judge(
                        prompt, baseline_reply["v"], res.get("reply", "")
                    )
                except Exception as e:
                    res["accuracy"] = {
                        "score": -1,
                        "reasoning": f"{type(e).__name__}",
                        "criteria": {},
                    }
            res["context_window"] = res.pop("raw_tool_outputs", [])
            yield _sse({"type": "column", "result": res})
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()

    yield _sse({"type": "done"})


@router.post("/demo/compare/stream")
async def demo_compare_stream(request: Request, body: DemoCompareRequest):
    """SSE variant of the visual bench: streams each of the 10 columns as it
    completes (up to 4 running in parallel), so the UI updates in real time."""
    user_id = _auth(request)
    from api import DEFAULT_MODEL, _collect_mcp_tools_async  # lazy
    from Tools import all_tools

    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt required")
    model_name = (body.model or DEFAULT_MODEL).strip() or DEFAULT_MODEL

    try:
        mcp_tools = await _collect_mcp_tools_async(user_id)
    except Exception as e:
        print(f"[/demo/compare/stream] MCP discovery failed: {e!r}", flush=True)
        mcp_tools = []
    safe_builtins = [t for t in all_tools if t.name in _DEMO_SAFE_BUILTINS]
    real_tools = safe_builtins + list(mcp_tools)

    return StreamingResponse(
        _visual_stream_events(body, user_id, real_tools, model_name),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


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
        try:
            return await _run_visual_compression_tab(
                body, user_id, real_tools, model_name
            )
        except HTTPException:
            raise
        except BaseException as e:
            # Last-ditch — surfaces as a structured 500 with a real
            # detail field so the UI's "unknown error" fallback never
            # fires. Stack trace also lands in the uvicorn log.
            import traceback
            from api import _unwrap_exc  # lazy, matches other call sites

            inner = _unwrap_exc(e) if isinstance(e, Exception) else e
            print(
                f"[/demo/compare] visual-compression orchestrator crashed: "
                f"{type(inner).__name__}: {inner}",
                flush=True,
            )
            traceback.print_exc()
            raise HTTPException(
                500,
                f"visual-compression run failed: "
                f"{type(inner).__name__}: {str(inner)[:300]}",
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
