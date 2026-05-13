"""Sandbox backend interface + concrete implementations.

Two backends behind one ABC, selected by the SANDBOX_BACKEND env var:
  - docker — local Docker socket. Solo-dev / localhost only (unsafe for multi-user).
  - e2b    — managed Firecracker microVMs (e2b.dev). Safe for multi-user.

Every other module that needs to run code inside a workspace (shell_tool,
file tools, git tools, GC loop) goes through `get_backend()` — never
imports DockerBackend or E2BBackend directly. This is the load-bearing
abstraction that turns the docker→e2b migration into a config flip
instead of a rewrite. See PROJECT.md for the threat model.
"""

from __future__ import annotations

import io
import logging
import os
import re
import tarfile
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ── Public types ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class SandboxError(Exception):
    """Base for all sandbox failures."""


class SandboxNotFoundError(SandboxError):
    """The referenced workspace doesn't exist (or was destroyed)."""


class SandboxTimeoutError(SandboxError):
    """An exec call exceeded its deadline. Not raised — returned as exit 124."""


# ── Abstract backend ─────────────────────────────────────────────────────────

class SandboxBackend(ABC):
    """A sandbox runtime. Implementations live below."""

    @abstractmethod
    def create(self, *, user_id: str, session_id: str) -> str:
        """Provision a fresh workspace and return its backend-specific ref
        (container id for docker, sandbox id for e2b)."""

    @abstractmethod
    def exec(
        self,
        ref: str,
        cmd,
        *,
        cwd: str = "/workspace",
        timeout: int = 60,
        env: Optional[dict] = None,
    ) -> ExecResult:
        """Run a command inside the workspace and wait for it. Resumes if paused."""

    @abstractmethod
    def read_file(self, ref: str, path: str) -> bytes:
        """Read a file from the workspace."""

    @abstractmethod
    def write_file(self, ref: str, path: str, data: bytes) -> None:
        """Write bytes to a file in the workspace, creating parents if needed."""

    @abstractmethod
    def pause(self, ref: str) -> None:
        """Suspend the workspace to free compute. Filesystem is preserved."""

    @abstractmethod
    def resume(self, ref: str) -> None:
        """Resume a paused workspace."""

    @abstractmethod
    def destroy(self, ref: str) -> None:
        """Tear down the workspace. Idempotent."""

    @abstractmethod
    def status(self, ref: str) -> str:
        """One of: 'running', 'paused', 'destroyed', 'unknown'."""


# ── Secret-safe logging helper ───────────────────────────────────────────────

# Matches the GitHub PAT formats: ghp_/ghs_/gho_/ghu_/ghr_/github_pat_
_PAT_RE = re.compile(
    r"\b(?:gh[posur]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b"
)


def _redact(s: str) -> str:
    return _PAT_RE.sub("<redacted-token>", s)


# ── Docker backend ───────────────────────────────────────────────────────────

class DockerBackend(SandboxBackend):
    """Backed by local Docker via the host socket. Solo-dev / localhost only.

    A container escape with the Docker socket reachable is host root. Do NOT
    use this backend with anyone other than yourself on localhost. Flip
    SANDBOX_BACKEND=e2b before multi-user. See PROJECT.md.
    """

    def __init__(self, image: Optional[str] = None):
        try:
            import docker  # local import keeps the e2b-only path lighter
        except ImportError as e:  # pragma: no cover
            raise SandboxError(
                "DockerBackend requires the 'docker' package "
                "(pip install -r backend/requirements.txt)"
            ) from e
        self._docker = docker
        self._client = docker.from_env()
        self._image = image or os.environ.get("WORKSPACE_IMAGE", "acm-workspace:latest")

    # -- lifecycle --

    def create(self, *, user_id: str, session_id: str) -> str:
        name = f"acm-ws-{uuid.uuid4().hex[:12]}"
        try:
            container = self._client.containers.run(
                self._image,
                command=["sleep", "infinity"],
                detach=True,
                name=name,
                labels={
                    "acm.workspace": "true",
                    "acm.user_id": user_id,
                    "acm.session_id": session_id,
                },
                mem_limit="1g",
                memswap_limit="1g",
                cpu_period=100000,
                cpu_quota=200000,   # 2 CPUs equivalent
                pids_limit=512,
                network_mode="bridge",
            )
            return container.id
        except self._docker.errors.ImageNotFound as e:
            raise SandboxError(
                f"Workspace image not found: {self._image}. "
                f"Build it with ./sandbox/build.sh"
            ) from e

    def pause(self, ref: str) -> None:
        try:
            self._get_container(ref).stop(timeout=5)
        except Exception as e:
            raise SandboxError(_redact(str(e))) from e

    def resume(self, ref: str) -> None:
        try:
            self._get_container(ref).start()
        except Exception as e:
            raise SandboxError(_redact(str(e))) from e

    def destroy(self, ref: str) -> None:
        try:
            container = self._client.containers.get(ref)
        except self._docker.errors.NotFound:
            return
        try:
            container.remove(force=True)
        except self._docker.errors.NotFound:
            return
        except Exception as e:
            raise SandboxError(_redact(str(e))) from e

    def status(self, ref: str) -> str:
        try:
            container = self._client.containers.get(ref)
        except self._docker.errors.NotFound:
            return "destroyed"
        s = container.status
        if s == "running":
            return "running"
        if s in {"exited", "created"}:
            return "paused"
        return s or "unknown"

    # -- exec + IO --

    def _ensure_running(self, container):
        """Start the container if it isn't already running.

        Docker's exec_run / get_archive / put_archive all require a live
        container. The GC loop pauses idle workspaces, so every IO entry
        point has to revive them first.
        """
        if container.status != "running":
            container.start()
            container.reload()

    def exec(self, ref, cmd, *, cwd="/workspace", timeout=60, env=None) -> ExecResult:
        container = self._get_container(ref)
        self._ensure_running(container)

        # Plain `bash -c` (not `-lc`) — login-shell init in this image runs a
        # clear_console exit hook that returns non-zero, which leaks into the
        # command's exit code and breaks `set -e` flows like the autocommit.
        cmd_list = ["bash", "-c", cmd] if isinstance(cmd, str) else list(cmd)

        # exec_run is blocking and not cancellable; we wrap it in a worker
        # thread to enforce timeout, then best-effort kill on overrun.
        out: dict = {}
        started = time.monotonic()

        def _run():
            try:
                res = container.exec_run(
                    cmd_list,
                    workdir=cwd,
                    environment=env or {},
                    demux=True,
                )
                out["result"] = res
            except Exception as e:
                out["error"] = e

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=timeout)
        elapsed_ms = int((time.monotonic() - started) * 1000)

        if t.is_alive():
            try:
                container.exec_run(["bash", "-c", "pkill -KILL -P 1 2>/dev/null || true"])
            except Exception:
                pass
            return ExecResult("", f"Command timed out after {timeout}s.", 124, elapsed_ms)

        if "error" in out:
            raise SandboxError(_redact(str(out["error"])))

        res = out["result"]
        if isinstance(res.output, tuple):
            stdout_b, stderr_b = res.output
        else:
            stdout_b, stderr_b = res.output, b""
        return ExecResult(
            stdout=_redact((stdout_b or b"").decode(errors="replace")),
            stderr=_redact((stderr_b or b"").decode(errors="replace")),
            exit_code=int(res.exit_code or 0),
            duration_ms=elapsed_ms,
        )

    def read_file(self, ref: str, path: str) -> bytes:
        container = self._get_container(ref)
        self._ensure_running(container)
        try:
            stream, _ = container.get_archive(path)
        except self._docker.errors.NotFound as e:
            raise SandboxNotFoundError(f"File not in workspace: {path}") from e

        buf = io.BytesIO(b"".join(stream))
        buf.seek(0)
        with tarfile.open(fileobj=buf) as tar:
            members = [m for m in tar.getmembers() if m.isfile()]
            if not members:
                raise SandboxNotFoundError(f"File not in workspace: {path}")
            f = tar.extractfile(members[0])
            return f.read() if f else b""

    def write_file(self, ref: str, path: str, data: bytes) -> None:
        container = self._get_container(ref)
        self._ensure_running(container)

        parent = os.path.dirname(path) or "/workspace"
        if parent and parent not in {"/", "/workspace"}:
            container.exec_run(["mkdir", "-p", parent])

        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w") as tar:
            info = tarfile.TarInfo(name=os.path.basename(path))
            info.size = len(data)
            info.mode = 0o644
            # Stamp current mtime: without this every write has mtime=epoch,
            # and git's stat-cache fast-path (same mtime+size = unchanged)
            # silently misses content edits on same-length files.
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
        tar_buf.seek(0)
        container.put_archive(parent, tar_buf.getvalue())

    # -- internal --

    def _get_container(self, ref: str):
        try:
            return self._client.containers.get(ref)
        except self._docker.errors.NotFound as e:
            raise SandboxNotFoundError(f"Workspace {ref} not found") from e


# ── E2B backend ──────────────────────────────────────────────────────────────

class E2BBackend(SandboxBackend):
    """Backed by E2B managed Firecracker microVMs (e2b.dev).

    Safe for multi-user (real microVM isolation). Requires E2B_API_KEY from
    https://e2b.dev/dashboard. See PROJECT.md.
    """

    def __init__(self, api_key: Optional[str] = None):
        try:
            from e2b import Sandbox  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise SandboxError(
                "E2BBackend requires the 'e2b' package "
                "(pip install -r backend/requirements.txt)"
            ) from e
        key = api_key or os.environ.get("E2B_API_KEY", "").strip()
        if not key:
            raise SandboxError(
                "E2B_API_KEY missing. Get one at https://e2b.dev/dashboard "
                "and set it in backend/.env, or switch SANDBOX_BACKEND=docker."
            )
        from e2b import Sandbox as _Sandbox
        self._Sandbox = _Sandbox
        self._api_key = key

    # E2B's Python SDK exposes Sandbox as the unit of work. We hold the
    # sandbox_id (ref) as the canonical handle and rehydrate via .connect()
    # on each call — paused sandboxes auto-resume on connect.

    def _connect(self, ref: str):
        try:
            return self._Sandbox.connect(ref, api_key=self._api_key)
        except Exception as e:
            raise SandboxNotFoundError(_redact(str(e))) from e

    def create(self, *, user_id: str, session_id: str) -> str:
        try:
            # The e2b SDK exposes `Sandbox.create(...)` as the classmethod for
            # provisioning a new sandbox; the bare `Sandbox(...)` constructor
            # only rehydrates an existing one and doesn't accept api_key or
            # metadata. We bake user/session identifiers into metadata so
            # E2B-side listings can find and tear down our sandboxes from
            # outside this process if needed.
            sbx = self._Sandbox.create(
                api_key=self._api_key,
                metadata={
                    "user_id": user_id,
                    "session_id": session_id,
                    "acm": "true",
                },
            )
        except Exception as e:
            raise SandboxError(_redact(str(e))) from e
        return sbx.sandbox_id

    def exec(self, ref, cmd, *, cwd="/workspace", timeout=60, env=None) -> ExecResult:
        sbx = self._connect(ref)
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        started = time.monotonic()
        try:
            res = sbx.commands.run(
                cmd_str, cwd=cwd, envs=env or {}, timeout=timeout,
            )
        except Exception as e:
            msg = str(e).lower()
            if "timeout" in msg or "deadline" in msg:
                return ExecResult(
                    "", f"Command timed out after {timeout}s.", 124,
                    int((time.monotonic() - started) * 1000),
                )
            raise SandboxError(_redact(str(e))) from e
        return ExecResult(
            stdout=_redact(getattr(res, "stdout", "") or ""),
            stderr=_redact(getattr(res, "stderr", "") or ""),
            exit_code=int(getattr(res, "exit_code", 0) or 0),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def read_file(self, ref: str, path: str) -> bytes:
        sbx = self._connect(ref)
        try:
            content = sbx.files.read(path)
        except Exception as e:
            raise SandboxNotFoundError(_redact(str(e))) from e
        return content if isinstance(content, (bytes, bytearray)) else str(content).encode()

    def write_file(self, ref: str, path: str, data: bytes) -> None:
        sbx = self._connect(ref)
        try:
            sbx.files.write(path, data)
        except Exception as e:
            raise SandboxError(_redact(str(e))) from e

    def pause(self, ref: str) -> None:
        sbx = self._connect(ref)
        try:
            sbx.pause()
        except Exception as e:
            raise SandboxError(_redact(str(e))) from e

    def resume(self, ref: str) -> None:
        # Sandbox.connect() on a paused sandbox auto-resumes it.
        self._connect(ref)

    def destroy(self, ref: str) -> None:
        try:
            sbx = self._Sandbox.connect(ref, api_key=self._api_key)
        except Exception:
            return
        try:
            sbx.kill()
        except Exception as e:
            raise SandboxError(_redact(str(e))) from e

    def status(self, ref: str) -> str:
        try:
            self._Sandbox.connect(ref, api_key=self._api_key)
            return "running"
        except Exception:
            return "destroyed"


# ── Factory ──────────────────────────────────────────────────────────────────

_backend: Optional[SandboxBackend] = None


def get_backend() -> SandboxBackend:
    """Return the configured backend, instantiated on first use."""
    global _backend
    if _backend is not None:
        return _backend
    choice = os.environ.get("SANDBOX_BACKEND", "docker").strip().lower()
    if choice == "docker":
        _backend = DockerBackend()
    elif choice == "e2b":
        _backend = E2BBackend()
    else:
        raise SandboxError(
            f"Unknown SANDBOX_BACKEND={choice!r}. Use 'docker' or 'e2b'."
        )
    logger.info("Sandbox backend initialised: %s", choice)
    return _backend


def reset_backend() -> None:
    """Drop the cached backend instance. For tests / config reload."""
    global _backend
    _backend = None
