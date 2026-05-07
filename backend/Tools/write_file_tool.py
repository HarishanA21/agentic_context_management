from langchain.tools import tool
from langchain_core.runnables import RunnableConfig

from Tools._paths import get_session_ids, safe_name
from storage import file_key, get_bucket

MAX_WRITE_BYTES = 1_000_000  # 1 MB cap


@tool
def write_project_file(
    filename: str, content: str, config: RunnableConfig
) -> str:
    """Write text content to a file in the current project's files.

    Creates the file if missing, overwrites it if it exists.

    Args:
        filename: The basename of the file to write (no directory components).
        content: The UTF-8 text content to write.
    """
    try:
        user_id, session_id = get_session_ids(config)
        name = safe_name(filename)
    except ValueError as e:
        return f"Error: {e}"

    if not isinstance(content, str):
        return "Error: content must be a string."

    payload = content.encode("utf-8")
    if len(payload) > MAX_WRITE_BYTES:
        return f"Error: content exceeds {MAX_WRITE_BYTES // 1024} KB limit."

    try:
        get_bucket().upload(
            path=file_key(user_id, session_id, name),
            file=payload,
            file_options={
                "content-type": "text/plain; charset=utf-8",
                "upsert": "true",
            },
        )
    except Exception as e:
        return f"Error writing file: {e}"

    return f"Wrote {len(payload)} bytes to {name}."
