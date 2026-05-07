from langchain.tools import tool
from langchain_core.runnables import RunnableConfig

from Tools._paths import get_session_ids
from storage import get_bucket, session_prefix


@tool
def list_project_files(config: RunnableConfig) -> str:
    """List the files uploaded to the current project.

    Returns each file's name and size, or "No files." if the project has none.
    """
    try:
        user_id, session_id = get_session_ids(config)
    except ValueError as e:
        return f"Error: {e}"

    try:
        items = get_bucket().list(session_prefix(user_id, session_id))
    except Exception as e:
        return f"Error: failed to list files: {e}"

    real = [it for it in (items or []) if it.get("id")]
    if not real:
        return "No files."
    lines = []
    for it in real:
        size = (it.get("metadata") or {}).get("size", 0)
        lines.append(f"- {it.get('name')} ({size} bytes)")
    return "\n".join(lines)
