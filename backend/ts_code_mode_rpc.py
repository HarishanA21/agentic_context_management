"""Per-request RPC server that the Deno isolate calls back into.

When ``execute_typescript`` runs the model's program, the program needs
a way to invoke real LangChain tools. The Deno isolate is locked down
to ``--allow-net=127.0.0.1:<port>`` only — no fs, no subprocess, no
other network — so the *only* exit it has is this loopback HTTP server.

Why a per-request server (and not a long-lived endpoint):
  * The token + port-rotation means one turn's script cannot accidentally
    (or maliciously) talk to another turn's RPC server.
  * The allowed-tools set is scoped to *this turn's described-names
    registry*. A tool the model never described isn't callable, which
    closes the "model invents a name in code" failure path.
  * Lifetime is exactly the lifetime of the Deno subprocess, so we
    don't keep stale config in memory.

Wire format (deliberately minimal):

  Request:  POST /rpc
            Authorization: Bearer <token>
            Content-Type:  application/json
            Body:          {"tool": "<safe_name>", "input": {...}}

  Response: 200 {"ok": true,  "value": <json>}
            403 {"ok": false, "error": "bad token"}
            404 {"ok": false, "error": "unknown / not allowed"}
            500 {"ok": false, "error": "<tool error>"}

The handler awaits ``tool.ainvoke(input, config)`` on the *current*
event loop. Since uvicorn is already running on the main loop, we
piggyback on it: no extra thread, no run_coroutine_threadsafe dance.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import socket
from typing import Any, Dict, Iterable, Optional

import uvicorn
from langchain_core.tools import BaseTool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ts_code_mode import sanitise_tool_name


def _find_free_port() -> int:
    """Bind a transient socket to grab a kernel-assigned port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _coerce_to_string(value: Any) -> str:
    """Best-effort to-string for whatever a tool returns.

    All our LangChain tools currently return ``str``, but MCP tools can
    return structured content (text blocks, JSON, even bytes). The
    isolate side expects ``{ text: string }`` per the TS API, so flatten
    anything else into a JSON string rather than failing the call.
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        return repr(value)


class ToolRpcServer:
    """Async context manager that runs a uvicorn server on 127.0.0.1.

    Usage::

        async with ToolRpcServer(allowed_tools, langchain_config) as rpc:
            # rpc.port + rpc.token are now valid; spawn Deno…
            ...
    """

    def __init__(
        self,
        allowed_tools: Iterable[BaseTool],
        langchain_config: Optional[Dict[str, Any]] = None,
    ):
        # Build a single lookup map. The allowed_names set is what we
        # *enforce*: a request for any name outside this set 404s.
        self._tools: Dict[str, BaseTool] = {}
        for t in allowed_tools:
            self._tools[sanitise_tool_name(t.name)] = t
        self._config = langchain_config or {}
        self.token: str = secrets.token_urlsafe(24)
        self.port: int = 0
        self._server: Optional[uvicorn.Server] = None
        self._serve_task: Optional[asyncio.Task[Any]] = None

    @property
    def allowed_names(self) -> set[str]:
        return set(self._tools.keys())

    async def _handle_rpc(self, request: Request) -> JSONResponse:
        # 1. token check (constant-time compare, header form: "Bearer …")
        auth = request.headers.get("authorization", "")
        provided = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not secrets.compare_digest(provided, self.token):
            return JSONResponse({"ok": False, "error": "bad token"}, status_code=403)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "invalid json body"}, status_code=400
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "error": "body must be a JSON object"},
                status_code=400,
            )

        tool_name = body.get("tool")
        tool_input = body.get("input") or {}
        if not isinstance(tool_name, str):
            return JSONResponse(
                {"ok": False, "error": "missing 'tool' string"}, status_code=400
            )
        if not isinstance(tool_input, dict):
            return JSONResponse(
                {"ok": False, "error": "'input' must be an object"},
                status_code=400,
            )

        # 2. allowed-names enforcement — model never described it, so refuse.
        tool = self._tools.get(tool_name)
        if tool is None:
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        f"tool {tool_name!r} is not in this turn's allow-list. "
                        f"Call describe_tools first."
                    ),
                },
                status_code=404,
            )

        # 3. dispatch — ainvoke is async-friendly across the board (sync
        #    LangChain tools work too: their ainvoke wraps invoke).
        try:
            result = await tool.ainvoke(tool_input, self._config)
        except Exception as e:
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                },
                status_code=500,
            )

        return JSONResponse({"ok": True, "value": _coerce_to_string(result)})

    def _build_app(self) -> Starlette:
        return Starlette(routes=[Route("/rpc", self._handle_rpc, methods=["POST"])])

    async def __aenter__(self) -> "ToolRpcServer":
        self.port = _find_free_port()
        cfg = uvicorn.Config(
            self._build_app(),
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        self._server = uvicorn.Server(cfg)
        # Avoid uvicorn installing its own SIGINT/SIGTERM handlers (would
        # clash with the parent FastAPI app's signal handling).
        self._server.install_signal_handlers = lambda: None  # type: ignore[assignment]
        self._serve_task = asyncio.create_task(self._server.serve())
        # Wait until uvicorn says it's ready before returning, so the
        # Deno subprocess doesn't connect-refuse on the first request.
        while not self._server.started:
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._serve_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(self._serve_task, timeout=5)
        self._server = None
        self._serve_task = None
