"""The first half of ts_code_mode's two-tool surface.

``describe_tools(names)`` is the agent's *lookup* tool. The agent reads
the prompt-resident catalog (just names + one-liners), picks which
tools it needs this turn, and calls describe_tools to fetch the full
TypeScript interfaces + JSDoc for those names. Anything it describes
is sticky for the rest of the thread — execute_typescript will only
wire shims for tools the thread has described.

Unknown names get a ``did you mean…`` reply built from
``difflib.get_close_matches`` so the model can self-correct in the
same turn rather than crashing into TS-compile errors later.
"""

from __future__ import annotations

import difflib
from typing import Iterable, List

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field


_DESCRIBE_DESCRIPTION = (
    "Fetch the TypeScript interface + JSDoc for one or more tool names. "
    "Call this BEFORE writing code that uses a tool. Names you describe "
    "are remembered for the rest of this conversation — no need to "
    "describe the same tool twice. Returns a TS code block you can "
    "consult for argument shapes."
)


class DescribeToolsInput(BaseModel):
    names: List[str] = Field(
        description=(
            "Tool names from the catalog. Use the sanitised form "
            "(underscores) — that's what the catalog shows."
        )
    )


def make_describe_tools_tool(real_tools: Iterable[BaseTool]) -> StructuredTool:
    """Build a `describe_tools` StructuredTool bound to the supplied
    real-tool list. The bound list is snapshotted so subsequent catalog
    edits don't change a live agent's behaviour mid-turn.
    """
    from ts_code_mode import (
        build_name_index,
        generate_ts_subset,
        resolve_names,
        sanitise_tool_name,
    )
    from ts_code_mode_registry import registry_add, thread_id_from_config

    tools_list = list(real_tools)
    index = build_name_index(tools_list)
    # The catalog of every available sanitised name — used for
    # did-you-mean suggestions when the model invents a name.
    every_name = sorted({sanitise_tool_name(t.name) for t in tools_list})

    async def _run(names: List[str], config: RunnableConfig) -> str:
        # Normalise: the model may send a single string by mistake or
        # wrap names in quotes.
        if isinstance(names, str):
            names = [names]
        cleaned = [n.strip() for n in names if isinstance(n, str) and n.strip()]
        if not cleaned:
            return "Error: describe_tools needs at least one tool name."

        found, unknown = resolve_names(cleaned, index)

        # Mark the found names as described BEFORE building the response,
        # so a concurrent execute_typescript in the same AIMessage sees
        # them as available.
        if found:
            tid = thread_id_from_config(config)
            await registry_add(tid, [sanitise_tool_name(t.name) for t in found])

        parts: list[str] = []
        if found:
            parts.append("```typescript")
            parts.append(generate_ts_subset(found))
            parts.append("```")
            parts.append("")
            parts.append(
                "These names are now usable inside execute_typescript "
                "(remembered for the rest of this conversation)."
            )

        if unknown:
            suggestions_lines = []
            for u in unknown:
                close = difflib.get_close_matches(u, every_name, n=5, cutoff=0.5)
                if close:
                    suggestions_lines.append(
                        f"  {u!r}: did you mean " + ", ".join(close) + "?"
                    )
                else:
                    suggestions_lines.append(
                        f"  {u!r}: no close match in the catalog."
                    )
            if parts:
                parts.append("")
            parts.append("Unknown names:")
            parts.extend(suggestions_lines)

        return "\n".join(parts) if parts else "(no result)"

    return StructuredTool.from_function(
        coroutine=_run,
        name="describe_tools",
        description=_DESCRIBE_DESCRIPTION,
        args_schema=DescribeToolsInput,
    )
