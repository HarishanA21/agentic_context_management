"""Verify the workspace-aware file tools (Step 2.5).

Drives `list_project_files`, `write_project_file`, `read_project_file`
against a real workspace and confirms:
  - writes land in /workspace, not S3
  - reads pull from /workspace
  - listing shows workspace files
  - chat-only sessions (no workspace_ref) still fall back to S3 cleanly
"""

from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
)

from psycopg_pool import ConnectionPool  # noqa: E402

import api  # noqa: E402
from sandbox_client import get_backend, reset_backend  # noqa: E402
from Tools.list_files_tool import list_project_files  # noqa: E402
from Tools.read_file_tool import read_project_file  # noqa: E402
from Tools.write_file_tool import write_project_file  # noqa: E402


def main() -> int:
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("SUPABASE_DB_URL not set"); return 1

    pool = ConnectionPool(db_url, min_size=1, max_size=2, kwargs={"autocommit": True})
    pool.wait()
    api.app.state.pool = pool

    reset_backend()
    backend = get_backend()

    user_id = str(uuid.uuid4())
    session_id, backend_ref = None, None

    try:
        with pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO sessions (user_id, name, kind) VALUES (%s, %s, %s) RETURNING id",
                (user_id, "file-tools-test", "project"),
            ).fetchone()
            session_id = str(row[0])

        backend_ref = backend.create(user_id=user_id, session_id=session_id)
        api._bootstrap_workspace(user_id, session_id, backend_ref)
        ws_config = {
            "configurable": {
                "user_id": user_id, "session_id": session_id,
                "workspace_ref": backend_ref,
            }
        }
        chat_config = {
            "configurable": {"user_id": user_id, "session_id": session_id}
        }

        print("\n[1] write_project_file → workspace")
        r = write_project_file.invoke(
            {"filename": "hello.py", "content": "print('hi from workspace')\n"},
            ws_config,
        )
        print(f"  {r}")
        assert "/workspace/hello.py" in r

        print("\n[2] file visible via shell (proves it landed in /workspace)")
        proof = backend.exec(backend_ref, "cat /workspace/hello.py", timeout=5)
        print(f"  cat: {proof.stdout.strip()!r}")
        assert "hi from workspace" in proof.stdout

        print("\n[3] read_project_file pulls from workspace")
        got = read_project_file.invoke({"filename": "hello.py"}, ws_config)
        print(f"  got: {got!r}")
        assert "hi from workspace" in got

        print("\n[4] list_project_files shows workspace contents")
        listing = list_project_files.invoke({}, ws_config)
        print(listing)
        assert "Workspace files" in listing
        assert "hello.py" in listing

        print("\n[5] read_project_file → not-found in workspace, falls through")
        msg = read_project_file.invoke({"filename": "nope.txt"}, ws_config)
        print(f"  {msg}")
        assert "not found" in msg.lower()

        print("\n[6] chat-only config (no workspace_ref): write/read against S3")
        # Use a unique filename to avoid clashing with anything in S3
        nm = f"chatonly-{uuid.uuid4().hex[:8]}.txt"
        r = write_project_file.invoke(
            {"filename": nm, "content": "s3 path"}, chat_config,
        )
        print(f"  write: {r}")
        assert "Wrote" in r and "/workspace/" not in r  # S3 path: no /workspace prefix
        got = read_project_file.invoke({"filename": nm}, chat_config)
        print(f"  read: {got!r}")
        assert got == "s3 path"
        # cleanup
        from storage import get_bucket, file_key
        get_bucket().remove([file_key(user_id, session_id, nm)])

        print("\n✓ all assertions passed")
        return 0
    except AssertionError as e:
        print(f"\n✗ assertion failed: {e}")
        return 1
    except Exception as e:
        print(f"\n✗ unexpected: {type(e).__name__}: {e}")
        import traceback; traceback.print_exc()
        return 1
    finally:
        if session_id:
            try:
                with pool.connection() as conn:
                    conn.execute("DELETE FROM sessions WHERE id=%s", (session_id,))
            except Exception:
                pass
        if backend_ref:
            try: backend.destroy(backend_ref)
            except Exception: pass
        pool.close()


if __name__ == "__main__":
    raise SystemExit(main())
