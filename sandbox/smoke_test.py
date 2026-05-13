"""Smoke test for the sandbox backend abstraction.

Exercises every method of the configured backend in order:
    create → exec → write_file → read_file → exec timeout → pause → resume → destroy

Run from repo root:
    SANDBOX_BACKEND=docker python -m sandbox.smoke_test
    SANDBOX_BACKEND=e2b    python -m sandbox.smoke_test

Note: the e2b path also requires E2B_API_KEY.
"""

from __future__ import annotations

import os
import sys
import time

# Allow running as `python -m sandbox.smoke_test` from repo root by adding
# backend/ to sys.path so `import sandbox_client` resolves.
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
)

from sandbox_client import get_backend, reset_backend  # noqa: E402


def _step(n: int, label: str) -> None:
    print(f"\n[{n}] {label}")


def main() -> int:
    backend_name = os.environ.get("SANDBOX_BACKEND", "docker")
    print(f"Smoke-testing SandboxBackend implementation: {backend_name!r}")

    reset_backend()
    backend = get_backend()

    ref = None
    try:
        _step(1, "create()")
        ref = backend.create(user_id="00000000-0000-0000-0000-000000000001",
                             session_id="00000000-0000-0000-0000-000000000002")
        print(f"  ref = {ref}")
        assert ref and isinstance(ref, str)
        assert backend.status(ref) == "running", f"status={backend.status(ref)!r}"

        _step(2, "exec() — basic command")
        r = backend.exec(ref, "echo hello && python -V")
        print(f"  exit_code={r.exit_code} stdout={r.stdout.strip()!r} time={r.duration_ms}ms")
        assert r.ok and "hello" in r.stdout and "Python" in r.stdout

        _step(3, "write_file() then read_file()")
        backend.write_file(ref, "/workspace/hello.txt", b"hi there\n")
        got = backend.read_file(ref, "/workspace/hello.txt")
        print(f"  wrote 9 bytes, read back {len(got)} bytes: {got!r}")
        assert got == b"hi there\n"

        _step(4, "exec() — read back via shell")
        r = backend.exec(ref, "cat /workspace/hello.txt")
        assert r.ok and r.stdout.strip() == "hi there"
        print(f"  cat: {r.stdout.strip()!r}")

        _step(5, "exec() — non-zero exit code propagates")
        r = backend.exec(ref, "false")
        print(f"  exit_code={r.exit_code}")
        assert r.exit_code != 0

        _step(6, "exec() — timeout returns 124")
        started = time.monotonic()
        r = backend.exec(ref, "sleep 30", timeout=2)
        elapsed = time.monotonic() - started
        print(f"  exit_code={r.exit_code} stderr={r.stderr.strip()!r} elapsed={elapsed:.2f}s")
        assert r.exit_code == 124
        assert elapsed < 5, "timeout did not enforce the deadline"

        _step(7, "pause() then status()")
        backend.pause(ref)
        s = backend.status(ref)
        print(f"  status after pause: {s!r}")
        assert s == "paused"

        _step(8, "resume() — verify filesystem survived")
        backend.resume(ref)
        assert backend.status(ref) == "running"
        r = backend.exec(ref, "cat /workspace/hello.txt")
        print(f"  post-resume cat: {r.stdout.strip()!r}")
        assert r.ok and r.stdout.strip() == "hi there"

        _step(9, "destroy() — idempotent")
        backend.destroy(ref)
        backend.destroy(ref)  # second call should be a no-op
        s = backend.status(ref)
        print(f"  status after destroy: {s!r}")
        assert s == "destroyed"
        ref = None

        print("\n✓ all checks passed")
        return 0
    except AssertionError as e:
        print(f"\n✗ assertion failed: {e}")
        return 1
    except Exception as e:
        print(f"\n✗ unexpected error: {type(e).__name__}: {e}")
        return 1
    finally:
        if ref is not None:
            try:
                backend.destroy(ref)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
