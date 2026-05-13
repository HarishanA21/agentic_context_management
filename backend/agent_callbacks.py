"""LangChain callback hooks for the agent.

Registered per /chat request so every tool call and LLM call gets timed
and logged to the backend terminal. Set AGENT_HOOKS=0 in .env to silence.

Output looks like:
    [14:23:45 a1b2c3] [llm  ] start
    [14:23:45 a1b2c3] [tool ] read_project_file args={"filename":"notes.md"}
    [14:23:45 a1b2c3] [tool ]     ->12ms |output="# Hello\\nworld"
    [14:23:48 a1b2c3] [llm  ]     ->3231ms |tokens=812
"""

from __future__ import annotations

import os
import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

ENABLED = os.environ.get("AGENT_HOOKS", "1") not in {"0", "false", "False", ""}


def _short(value: Any, n: int = 200) -> str:
    s = str(value)
    s = s.replace("\n", "\\n")
    return s if len(s) <= n else s[: n - 3] + "..."


class AgentLogger(BaseCallbackHandler):
    """Per-request callback that logs tool + LLM activity with timings."""

    def __init__(self, request_id: str = ""):
        self.request_id = (request_id or "")[:6] or "------"
        self._timers: dict[str, float] = {}

    # ─── helpers ──────────────────────────────────────────────────────────
    def _stamp(self, kind: str) -> str:
        return f"[{time.strftime('%H:%M:%S')} {self.request_id}] [{kind:<5}]"

    def _start(self, run_id: UUID) -> None:
        self._timers[str(run_id)] = time.perf_counter()

    def _ms(self, run_id: UUID) -> int:
        t0 = self._timers.pop(str(run_id), time.perf_counter())
        return int((time.perf_counter() - t0) * 1000)

    # ─── tool ─────────────────────────────────────────────────────────────
    def on_tool_start(
        self,
        serialized: dict,
        input_str: str,
        *,
        run_id: UUID,
        inputs: dict | None = None,
        **kwargs: Any,
    ) -> None:
        if not ENABLED:
            return
        name = (serialized or {}).get("name", "?")
        args = inputs if inputs is not None else input_str
        self._start(run_id)
        print(f"{self._stamp('tool')} {name} args={_short(args, 240)}", flush=True)

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        if not ENABLED:
            return
        ms = self._ms(run_id)
        out = getattr(output, "content", output)
        print(
            f"{self._stamp('tool')}     ->{ms}ms |output={_short(out, 200)}",
            flush=True,
        )

    def on_tool_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: Any
    ) -> None:
        if not ENABLED:
            return
        ms = self._ms(run_id)
        print(
            f"{self._stamp('tool')}     ->{ms}ms |ERROR {type(error).__name__}: {error}",
            flush=True,
        )

    # ─── llm / chat model ────────────────────────────────────────────────
    @staticmethod
    def _model_name(serialized: dict | None) -> str:
        s = serialized or {}
        # ChatOpenAI surfaces its model name in kwargs; fall back to the class.
        model = (s.get("kwargs") or {}).get("model") or (s.get("kwargs") or {}).get(
            "model_name"
        )
        if model:
            return str(model)
        ident = s.get("id")
        if isinstance(ident, list) and ident:
            return str(ident[-1])
        return s.get("name") or "?"

    def on_llm_start(
        self,
        serialized: dict,
        prompts: list[str],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        if not ENABLED:
            return
        self._start(run_id)
        print(
            f"{self._stamp('llm')} {self._model_name(serialized)} start ({len(prompts)} prompt(s))",
            flush=True,
        )

    def on_chat_model_start(
        self,
        serialized: dict,
        messages: list,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        if not ENABLED:
            return
        self._start(run_id)
        n = len(messages[0]) if messages else 0
        print(
            f"{self._stamp('llm')} {self._model_name(serialized)} start ({n} msg)",
            flush=True,
        )

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        if not ENABLED:
            return
        ms = self._ms(run_id)
        usage = ""
        try:
            meta = getattr(response, "llm_output", None) or {}
            tu = meta.get("token_usage") or meta.get("usage") or {}
            if tu:
                tot = tu.get("total_tokens") or (
                    (tu.get("prompt_tokens") or tu.get("input_tokens") or 0)
                    + (tu.get("completion_tokens") or tu.get("output_tokens") or 0)
                )
                if tot:
                    usage = f" |tokens={tot}"
        except Exception:
            pass
        print(f"{self._stamp('llm')}     ->{ms}ms{usage}", flush=True)

    def on_llm_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: Any
    ) -> None:
        if not ENABLED:
            return
        ms = self._ms(run_id)
        print(
            f"{self._stamp('llm')}     ->{ms}ms |ERROR {type(error).__name__}: {error}",
            flush=True,
        )
