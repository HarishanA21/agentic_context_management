"""The execution half of ts_code_mode's two-tool surface.

``execute_typescript`` runs the model's TypeScript program in a Deno
subprocess and returns its stdout to the LLM. Tools the program can
call are restricted to whatever the thread has described via
``describe_tools`` — the registry, not the full tool list, drives
which shims show up on the ``codemode`` object inside the program.
"""

from __future__ import annotations

from typing import Iterable

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field


_EXECUTE_DESCRIPTION = (
    "Run a TypeScript program that uses the project's tool API and "
    "return its stdout + stderr. Tools must be described via "
    "describe_tools first; only described tools are callable as "
    "`codemode.<name>(...)` inside the program. Use console.log for "
    "everything you want returned. 30s wall clock, 16 KB output cap."
)


class ExecuteTypescriptInput(BaseModel):
    code: str = Field(
        description=(
            "TypeScript source. Top-level await is supported. Call "
            "codemode.<tool_name>({...}) for any tool you've described. "
            "Use console.log to send results back."
        )
    )


def make_execute_typescript_tool(real_tools: Iterable[BaseTool]) -> StructuredTool:
    """Build the execute_typescript StructuredTool.

    Closure captures the full real-tool list, but at call time the
    runner intersects it with the thread's described-names registry
    so undescribed tools aren't shimmed.
    """
    # Snapshot so a later catalog change doesn't mutate a live agent.
    name_to_tool: dict[str, BaseTool] = {}
    from ts_code_mode import sanitise_tool_name

    for t in real_tools:
        name_to_tool[sanitise_tool_name(t.name)] = t

    async def _run(code: str, config: RunnableConfig) -> str:
        from ts_code_mode_registry import registry_get, thread_id_from_config
        from ts_code_mode_runner import execute_typescript_code

        thread_id = thread_id_from_config(config)
        described = await registry_get(thread_id)
        if not described:
            return (
                "Error: no tools described yet in this conversation. "
                "Call describe_tools(['tool_name', ...]) first, then "
                "execute_typescript."
            )
        allowed = [name_to_tool[n] for n in described if n in name_to_tool]
        if not allowed:
            return (
                "Error: the described names don't match any current tool. "
                "The tool catalog may have changed (e.g. an MCP was "
                "disabled). Call describe_tools again."
            )
        return await execute_typescript_code(code, allowed, config)

    return StructuredTool.from_function(
        coroutine=_run,
        name="execute_typescript",
        description=_EXECUTE_DESCRIPTION,
        args_schema=ExecuteTypescriptInput,
    )
