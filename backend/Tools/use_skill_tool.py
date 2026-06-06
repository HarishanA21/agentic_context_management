"""The ``read_skill`` tool — progressive disclosure for skills.

The agent's system prompt lists the available skills as JSON (names +
descriptions only — see ``skills_catalog.build_skills_system_prompt``). When a
task matches a skill, the model calls ``read_skill(skill_name)`` to load that
skill's full instructions on demand. Because it's a real tool call, it also
surfaces in the UI as a visible "Skill: <name>" step — the user can see which
skill ran.

``make_read_skill_tool`` binds the tool to a snapshot of the user's enabled
skills ({name: instructions}); the agent cache key includes a hash of that
snapshot, so toggling/editing a skill rebuilds the agent with a fresh tool.
"""

from __future__ import annotations

from typing import Dict

from langchain.tools import tool


def make_read_skill_tool(index: Dict[str, str]):
    """Build a ``read_skill`` tool that resolves ``skill_name`` against ``index``
    ({skill_name: full_instructions})."""
    available = ", ".join(sorted(index)) or "(none)"

    @tool
    def read_skill(skill_name: str) -> str:
        """Load the full instructions for one of the available skills, then
        follow them. Call this as soon as the user's task matches a skill listed
        under AVAILABLE SKILLS in your system prompt.

        Args:
            skill_name: The exact name of the skill to load (e.g. 'canvas-design').
        """
        key = (skill_name or "").strip()
        if key in index:
            return f"Skill '{key}' loaded — follow these instructions:\n\n{index[key]}"
        # case-insensitive fallback
        for name, instructions in index.items():
            if name.lower() == key.lower():
                return (
                    f"Skill '{name}' loaded — follow these instructions:\n\n"
                    f"{instructions}"
                )
        return (
            f"No available skill named '{skill_name}'. Available skills: "
            f"{available}."
        )

    return read_skill
