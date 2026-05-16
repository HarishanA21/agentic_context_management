"""The `delegate_to_subagent` LangChain tool.

Closes over the parent's tools + chat_model + tool surface so it can
build a brand-new agent on demand. Returns ``<summary>`` text from the
subagent's last assistant message; everything else stays isolated.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field


_DELEGATE_DESCRIPTION = (
    "Delegate an exploratory sub-task to a fresh sub-agent with its own "
    "context window. The sub-agent investigates and returns a SHORT summary; "
    "nothing else bleeds back to you. Use this when the path to the answer "
    "is much bigger than the answer itself (e.g. 'find which of these 20 "
    "files is relevant'). Parallel calls are fine — issue several in one "
    "assistant message to fan out."
)


class DelegateInput(BaseModel):
    task: str = Field(
        description=(
            "What the sub-agent should accomplish. Be specific about what "
            "it should investigate and what kind of answer you expect back."
        )
    )
    focus: Optional[str] = Field(
        default=None,
        description=(
            "Optional steering hint: which area to concentrate on, what to "
            "skip, what kind of detail to preserve in the summary."
        ),
    )


def _maybe_log(
    config: Optional[Dict[str, Any]], freed_tokens: int, details: Dict[str, Any]
) -> None:
    """Best-effort context_events entry so the UI shows subagent activity."""
    try:
        from api import _record_context_event, app  # lazy

        cfg = (config or {}).get("configurable", {}) or {}
        user_id = str(cfg.get("user_id") or "")
        session_id = str(cfg.get("session_id") or "")
        thread_id = str(cfg.get("thread_id") or session_id)
        if not (user_id and session_id and thread_id):
            return
        with app.state.pool.connection() as conn:
            _record_context_event(
                conn,
                user_id=user_id,
                session_id=session_id,
                thread_id=thread_id,
                turn_index=0,
                edit_type="subagent_call",
                freed_tokens=freed_tokens,
                details=details,
            )
    except Exception:
        pass


def make_delegate_tool(
    *,
    real_tools: List[BaseTool],
    tool_surface: str,
    chat_model: Any,
    base_system_prompt: str,
    token_budget: int,
    max_depth: int,
) -> StructuredTool:
    """Build the delegate_to_subagent tool. The chat_model and tool list
    are bound at agent-build time — same surface the parent has, so the
    subagent makes apples-to-apples decisions."""

    async def _run(
        task: str,
        config: RunnableConfig,
        focus: Optional[str] = None,
    ) -> str:
        from subagent import spawn_subagent_async  # lazy

        if not task or not task.strip():
            return "Error: task is required."

        result = await spawn_subagent_async(
            task=task,
            focus=focus,
            parent_config=config,
            real_tools=real_tools,
            tool_surface=tool_surface,
            chat_model=chat_model,
            base_system_prompt=base_system_prompt,
            token_budget=token_budget,
            max_depth=max_depth,
        )

        summary = result.get("summary", "")
        metrics = result.get("metrics", {}) or {}
        in_tok = int(metrics.get("input_tokens") or 0)
        out_tok = int(metrics.get("output_tokens") or 0)
        tool_calls = int(metrics.get("tool_calls") or 0)

        # The "freed tokens" semantic here is: how much context the
        # parent did NOT need to absorb. We approximate as the
        # subagent's input+output minus the summary it returned.
        summary_tokens = max(1, len(summary) // 4)
        freed = max(0, in_tok + out_tok - summary_tokens)
        _maybe_log(
            config,
            freed,
            {
                "task_preview": task[:120],
                "focus": (focus or "")[:80],
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "tool_calls": tool_calls,
                "summary_tokens": summary_tokens,
                "ok": bool(result.get("ok", True)),
            },
        )

        # Return a single short string. The orchestrator pattern says
        # only the summary crosses back; metrics live in context_events.
        return summary or "(sub-agent produced no summary)"

    return StructuredTool.from_function(
        coroutine=_run,
        name="delegate_to_subagent",
        description=_DELEGATE_DESCRIPTION,
        args_schema=DelegateInput,
    )
