"""Sub-agent spawning — technique B4 from CONTEXT_STRATEGIES_PLAN.md.

Lets the parent agent delegate a heavy exploratory sub-task to a
brand-new agent with its own context window. The subagent does the
expensive work, wraps its conclusion in `<summary>…</summary>`, and
returns just that string to the parent. None of the intermediate
exploration bleeds upward.

Why it matters:
  - Parent context stays small.
  - The subagent can fan out into 20 file reads without bloating
    anyone else's window.
  - Subagents can be parallel — the parent can issue several
    `delegate_to_subagent` tool calls in one assistant message.

What this module does NOT do:
  - It does not own its own LLM choice. The parent's resolved
    chat_model is reused so the subagent inherits the user's active
    provider + credentials.
  - It does not persist anything. Subagents always use
    `InMemorySaver`; nothing leaks into Postgres or chat history.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver


SUBAGENT_PROMPT_RIDER = (
    "\n\n[You are a focused sub-agent.\n"
    "  - You receive ONE task and must return ONE short answer.\n"
    "  - Do whatever investigation you need; you have full tool access.\n"
    "  - Your final assistant message MUST wrap the answer in "
    "<summary>...</summary>. Keep it under 500 tokens.\n"
    "  - Do not greet, do not narrate, do not ask follow-up questions. "
    "Investigate, then output the summary.]"
)


_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.S | re.I)


class SubagentBudgetError(RuntimeError):
    """Raised when the subagent exceeded its recursion / token cap."""


def _extract_summary(result_messages: List[Any]) -> str:
    """Pull the most recent assistant message and either extract the
    contents of its <summary> tag or return the raw text as fallback."""
    last_ai = ""
    for m in result_messages:
        if isinstance(m, AIMessage):
            content = m.content if isinstance(m.content, str) else str(m.content)
            if content.strip():
                last_ai = content
    if not last_ai:
        return "(subagent produced no response)"
    match = _SUMMARY_RE.search(last_ai)
    if match:
        return match.group(1).strip()
    return last_ai.strip()


def _build_subagent(
    tool_surface: str,
    real_tools: List[BaseTool],
    chat_model: Any,
    base_system_prompt: str,
):
    """Construct a one-off agent matching the parent's tool surface."""
    saver = InMemorySaver()
    if tool_surface == "ts_code_mode":
        from Tools.describe_tools_tool import make_describe_tools_tool
        from Tools.execute_typescript_tool import make_execute_typescript_tool
        from ts_code_mode import ts_code_mode_system_prompt

        agent_tools: List[BaseTool] = [
            make_describe_tools_tool(real_tools),
            make_execute_typescript_tool(real_tools),
        ]
        system_prompt = ts_code_mode_system_prompt(real_tools)
    else:
        agent_tools = list(real_tools)
        system_prompt = base_system_prompt
    system_prompt = system_prompt + SUBAGENT_PROMPT_RIDER
    return create_agent(
        model=chat_model,
        tools=agent_tools,
        system_prompt=system_prompt,
        checkpointer=saver,
    )


def _budget_to_recursion(token_budget: int) -> int:
    """Translate the plan's "token_budget" knob into LangGraph's
    `recursion_limit` (number of node steps). A rough rule of thumb:
    every 1k tokens ≈ 1 model+tool round-trip. Clamp to [5, 80] so the
    subagent has room to be useful without runaway loops."""
    return max(5, min(80, token_budget // 1000))


async def spawn_subagent_async(
    *,
    task: str,
    focus: Optional[str],
    parent_config: Dict[str, Any],
    real_tools: List[BaseTool],
    tool_surface: str,
    chat_model: Any,
    base_system_prompt: str,
    token_budget: int,
    max_depth: int,
) -> Dict[str, Any]:
    """Run a sub-agent end-to-end and return ``{summary, metrics}``."""
    parent_cfg = (parent_config or {}).get("configurable", {}) or {}
    current_depth = int(parent_cfg.get("subagent_depth") or 0)
    if current_depth >= max_depth:
        raise SubagentBudgetError(
            f"subagent max_depth ({max_depth}) reached — refusing to spawn"
        )

    rid = uuid.uuid4().hex[:8]
    # Fresh thread; inherit user/session for tool-config compatibility,
    # but *never* the workspace_ref — the subagent shouldn't mutate the
    # parent's workspace by accident.
    sub_config: Dict[str, Any] = {
        "configurable": {
            "thread_id": f"subagent:{parent_cfg.get('thread_id','?')}:{rid}",
            "user_id": parent_cfg.get("user_id"),
            "session_id": parent_cfg.get("session_id"),
            "session_mode": "auto",
            "subagent_depth": current_depth + 1,
        },
        "recursion_limit": _budget_to_recursion(token_budget),
    }

    sub_agent = _build_subagent(tool_surface, real_tools, chat_model, base_system_prompt)

    prompt_lines = [task]
    if focus:
        prompt_lines.append(f"\nFocus on: {focus}")
    prompt_lines.append(
        "\nWhen finished, wrap your answer in <summary>...</summary>. "
        "Do not include anything outside the tags."
    )
    prompt = "\n".join(prompt_lines)

    try:
        result = await sub_agent.ainvoke(
            {"messages": [HumanMessage(content=prompt)]},
            config=sub_config,
        )
    except Exception as e:
        # Surface as a structured result rather than letting the parent
        # see a raw stack trace.
        return {
            "summary": f"Sub-agent failed: {type(e).__name__}: {str(e)[:300]}",
            "ok": False,
            "metrics": {"tool_calls": 0, "input_tokens": 0, "output_tokens": 0},
        }

    messages = result.get("messages") or []
    summary = _extract_summary(messages)

    # Roll up usage_metadata across every AIMessage in the run.
    input_tokens = 0
    output_tokens = 0
    tool_calls = 0
    for m in messages:
        if isinstance(m, AIMessage):
            um = getattr(m, "usage_metadata", None) or {}
            input_tokens += int(um.get("input_tokens") or 0)
            output_tokens += int(um.get("output_tokens") or 0)
            tool_calls += len(m.tool_calls or [])

    return {
        "summary": summary,
        "ok": True,
        "metrics": {
            "tool_calls": tool_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "messages": len(messages),
        },
    }
