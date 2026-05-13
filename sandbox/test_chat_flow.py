"""End-to-end test of the Step 1.7 wiring.

Mimics what /chat does for a project session, without going through the
HTTP layer or LangGraph:
    1. Create a `kind='project'` session row.
    2. Call `_ensure_workspace_for_session()` → workspace exists, ref returned.
    3. Verify the workspaces row is healthy.
    4. Call run_shell with the workspace_ref injected into the runnable config.
    5. Confirm we can write a file, then read it back via shell.
    6. Confirm the helper reuses the existing workspace on a second call.
    7. Clean up: delete the session (cascades to workspace row, destroys the
       container).

Run from repo root:
    SANDBOX_BACKEND=docker \\
    SUPABASE_DB_URL=postgresql://postgres:postgres@localhost:5433/acm \\
    .venv/bin/python -m sandbox.test_chat_flow
"""

from __future__ import annotations

import os
import sys
import uuid

# Make `import api` resolve to backend/api.py.
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
)

from psycopg_pool import ConnectionPool  # noqa: E402

import api  # noqa: E402
from sandbox_client import get_backend, reset_backend  # noqa: E402
from Tools.shell_tool import run_shell  # noqa: E402


def main() -> int:
    backend_name = os.environ.get("SANDBOX_BACKEND", "docker")
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("Error: SUPABASE_DB_URL not set.")
        return 1

    print(f"Chat-flow integration test with SANDBOX_BACKEND={backend_name!r}")

    # Stand up the same ConnectionPool that lifespan() uses, and patch it onto
    # api.app.state so the helper can read app.state.pool.
    pool = ConnectionPool(db_url, min_size=1, max_size=2, kwargs={"autocommit": True})
    pool.wait()
    api.app.state.pool = pool

    reset_backend()
    backend = get_backend()

    user_id = str(uuid.uuid4())
    session_id = None
    workspace_ref = None

    try:
        # 1. Create a project session.
        with pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO sessions (user_id, name, kind) "
                "VALUES (%s, %s, %s) RETURNING id",
                (user_id, "chat-flow-test", "project"),
            ).fetchone()
            session_id = str(row[0])
        print(f"\n[1] session_id = {session_id}")

        # 2. Lazy-create the workspace.
        ws_dict, workspace_ref = api._ensure_workspace_for_session(user_id, session_id)
        print(f"[2] workspace_id = {ws_dict['id']}")
        print(f"    status       = {ws_dict['status']}")
        print(f"    backend      = {ws_dict['backend']}")
        print(f"    backend_ref  = {workspace_ref[:24]}…")
        assert ws_dict["status"] == "running"
        assert backend.status(workspace_ref) == "running"

        # 3. Verify the workspaces row landed correctly.
        with pool.connection() as conn:
            cnt = conn.execute(
                "SELECT count(*) FROM workspaces WHERE session_id=%s AND status='running'",
                (session_id,),
            ).fetchone()[0]
        print(f"[3] DB row count for this session = {cnt}")
        assert cnt == 1

        # 4. Use run_shell with the workspace_ref injected into config.
        config = {"configurable": {"workspace_ref": workspace_ref}}
        out = run_shell.invoke(
            {"cmd": "echo hello from agent && pwd && whoami"}, config
        )
        print(f"[4] run_shell output:\n{out}")
        assert "hello from agent" in out
        assert "/workspace" in out
        assert "agent" in out  # the non-root user from the Dockerfile

        # 5. Round-trip a file through shell.
        run_shell.invoke({"cmd": "echo 'phase-1 wiring works' > /workspace/proof.txt"}, config)
        out = run_shell.invoke({"cmd": "cat /workspace/proof.txt"}, config)
        print(f"[5] file round-trip:\n{out}")
        assert "phase-1 wiring works" in out

        # 6. Helper must reuse the existing workspace on a second call.
        ws_dict2, ref2 = api._ensure_workspace_for_session(user_id, session_id)
        print(f"[6] second call reuses ref? {ref2 == workspace_ref}")
        assert ref2 == workspace_ref
        assert ws_dict2["id"] == ws_dict["id"]

        # Helper should also revive a paused workspace.
        backend.pause(workspace_ref)
        assert backend.status(workspace_ref) == "paused"
        ws_dict3, ref3 = api._ensure_workspace_for_session(user_id, session_id)
        print(f"[6.b] paused-workspace revive: {ws_dict3['status']!r} (expected 'running')")
        assert ref3 == workspace_ref
        assert ws_dict3["status"] == "running"
        assert backend.status(workspace_ref) == "running"

        print("\n✓ all checks passed")
        return 0

    except AssertionError as e:
        print(f"\n✗ assertion failed: {e}")
        return 1
    except Exception as e:
        print(f"\n✗ unexpected error: {type(e).__name__}: {e}")
        return 1
    finally:
        # CASCADE delete the session — destroys the workspaces row too.
        # Then explicitly destroy the container so we don't leak.
        if session_id:
            try:
                with pool.connection() as conn:
                    conn.execute("DELETE FROM sessions WHERE id=%s", (session_id,))
            except Exception as e:
                print(f"  cleanup: session delete failed: {e}")
        if workspace_ref:
            try:
                backend.destroy(workspace_ref)
            except Exception:
                pass
        pool.close()


if __name__ == "__main__":
    raise SystemExit(main())
