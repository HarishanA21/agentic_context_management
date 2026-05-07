"""
Tools package — add new tool files here and import them below.
The agent will automatically get access to everything in `all_tools`.
"""

from Tools.calculator_tool import calculator
from Tools.list_files_tool import list_project_files
from Tools.read_file_tool import read_project_file
from Tools.weather_tool import get_weather
from Tools.write_file_tool import write_project_file

# Register every tool here — agent picks them all up automatically.
all_tools = [
    get_weather,
    calculator,
    list_project_files,
    read_project_file,
    write_project_file,
]
