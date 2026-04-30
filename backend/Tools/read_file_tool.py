from langchain.tools import tool
from langchain_core.runnables import RunnableConfig

from Tools._paths import safe_name, session_dir

MAX_READ_BYTES = 200_000  # 200 KB cap to keep tool responses small


@tool
def read_project_file(filename: str, config: RunnableConfig) -> str:
    """Read a UTF-8 text file from the current project's uploaded files.

    Args:
        filename: The basename of the file to read (no directory components).
    """
    try:
        sdir = session_dir(config)
        name = safe_name(filename)
    except ValueError as e:
        return f"Error: {e}"

    target = (sdir / name).resolve()
    if sdir not in target.parents:
        return "Error: invalid path."
    if not target.exists() or not target.is_file():
        return f"Error: '{name}' not found in project files."

    try:
        data = target.read_bytes()
    except OSError as e:
        return f"Error reading file: {e}"

    truncated = len(data) > MAX_READ_BYTES
    if truncated:
        data = data[:MAX_READ_BYTES]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return f"Error: '{name}' is not a UTF-8 text file."

    return text + ("\n\n[... truncated ...]" if truncated else "")
