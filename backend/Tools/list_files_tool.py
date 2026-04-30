from langchain.tools import tool
from langchain_core.runnables import RunnableConfig

from Tools._paths import session_dir


@tool
def list_project_files(config: RunnableConfig) -> str:
    """List the files uploaded to the current project.

    Returns each file's name and size, or "No files." if the project has none.
    """
    try:
        sdir = session_dir(config)
    except ValueError as e:
        return f"Error: {e}"
    files = sorted(p for p in sdir.iterdir() if p.is_file())
    if not files:
        return "No files."
    return "\n".join(f"- {p.name} ({p.stat().st_size} bytes)" for p in files)
