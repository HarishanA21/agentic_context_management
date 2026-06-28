"""
Tools package — add new tool files here and import them below.
The agent will automatically get access to everything in `all_tools`.
"""

from Tools.calculator_tool import calculator
from Tools.jit_tools import find_files, grep_files, head_file, tail_file
from Tools.list_files_tool import list_project_files
from Tools.read_file_tool import read_project_file
from Tools.shell_tool import run_shell
from Tools.weather_tool import get_weather
from Tools.write_file_tool import write_project_file

# Register every always-on tool here — agent picks them all up automatically.
all_tools = [
    get_weather,
    calculator,
    list_project_files,
    read_project_file,
    write_project_file,
    run_shell,
]

# Just-in-time retrieval primitives (technique B5). These are opt-in: they're
# only attached to the agent when the active profile sets
# `context_management.jit_tools.enabled`. Gating them behind the toggle is what
# makes the JIT-tools technique A/B-testable — see api.build_agent.
jit_retrieval_tools = [
    find_files,
    head_file,
    tail_file,
    grep_files,
]
