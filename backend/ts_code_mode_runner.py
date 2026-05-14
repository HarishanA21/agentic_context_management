"""Run the model's TypeScript program in a sandboxed Deno subprocess.

The runner is the *active* half of ts_code_mode:
  1. Look up which tools the current LangGraph thread has described
     (per the in-process registry).
  2. Spin up a ToolRpcServer scoped to that allow-list.
  3. Materialise a temp dir with two files:
        * codemode.ts — a generated stub that exposes
          ``codemode.<safe_name>(input)`` as fetch-backed shims pointing
          at the loopback RPC port + carrying the per-call token.
        * script.ts  — the model's code, wrapped in an async IIFE that
          imports codemode and runs.
  4. ``deno run --no-prompt --allow-net=127.0.0.1:<port> script.ts``
     with NO other --allow-* flags → no fs, no read, no write, no run,
     no env, no ffi. The only egress is the loopback RPC server.
  5. Capture stdout + stderr with a wall-clock timeout. Return a
     formatted string to the LLM.

The 30 s budget covers the *entire* program, not each tool call — the
plan's other Code Modes use the same convention.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from typing import Any, Dict, Iterable, Optional

from langchain_core.tools import BaseTool

from ts_code_mode import sanitise_tool_name
from ts_code_mode_rpc import ToolRpcServer


WALL_CLOCK_SECONDS = 30
GRACE_SECONDS = 5
MAX_OUTPUT_BYTES = 16_384


# ─── codemode.ts stub generation ──────────────────────────────────────────


_CODEMODE_TS_TEMPLATE = """\
// Auto-generated for one ts_code_mode turn. Do not commit.
// The only network the runtime allows is 127.0.0.1:{port}; the RPC
// server there is gated by the bearer token below and only accepts
// tool names the model has described this turn.

const __PORT = {port};
const __TOKEN = {token_json};

async function __call(tool: string, input: unknown): Promise<{{ text: string }}> {{
  const res = await fetch(`http://127.0.0.1:${{__PORT}}/rpc`, {{
    method: "POST",
    headers: {{
      "content-type": "application/json",
      "authorization": `Bearer ${{__TOKEN}}`,
    }},
    body: JSON.stringify({{ tool, input: input ?? {{}} }}),
  }});
  const body = await res.json().catch(() => null);
  if (!res.ok || !body || body.ok === false) {{
    const msg = body?.error ?? `${{res.status}} ${{res.statusText}}`;
    throw new Error(`codemode.${{tool}}: ${{msg}}`);
  }}
  return {{ text: typeof body.value === "string" ? body.value : String(body.value) }};
}}

export const codemode = {{
{shims}
}} as const;
"""


def _generate_codemode_ts(
    allowed_tools: Iterable[BaseTool], port: int, token: str
) -> str:
    shims: list[str] = []
    for t in allowed_tools:
        safe = sanitise_tool_name(t.name)
        shims.append(
            f"  {safe}: (input: Record<string, unknown>) => __call({safe!r}, input),"
        )
    return _CODEMODE_TS_TEMPLATE.format(
        port=port,
        token_json=json.dumps(token),
        shims="\n".join(shims) if shims else "  // (no tools yet — call describe_tools)",
    )


# ─── script.ts assembly ─────────────────────────────────────────────────


_FORBIDDEN_IMPORT_RE = re.compile(
    r'\b(?:import|require)\b[^\n]*?["\'](?:node:|deno:|jsr:|npm:|https?:)',
    re.IGNORECASE,
)


def _assemble_script(user_code: str) -> str:
    """Wrap the user's code in an async IIFE that pulls in codemode."""
    if _FORBIDDEN_IMPORT_RE.search(user_code):
        # Defensive: the runtime would block these anyway (no --allow-net
        # for non-loopback, no --allow-read for filesystem) but rejecting
        # here gives a cleaner error than a Deno permission denial.
        return (
            'import { codemode } from "./codemode.ts";\n'
            'void codemode;\n'
            'throw new Error("import of external modules (node:/deno:/jsr:/npm:/http) '
            'is blocked. Use the codemode API only.");\n'
        )
    return (
        'import { codemode } from "./codemode.ts";\n'
        "void codemode;\n"
        "(async () => {\n"
        f"{user_code}\n"
        "})().catch((e) => {\n"
        '  console.error(`Uncaught: ${e?.message ?? e}`);\n'
        "  if (e?.stack) console.error(e.stack);\n"
        "  Deno.exit(1);\n"
        "});\n"
    )


# ─── output helpers ─────────────────────────────────────────────────────


def _truncate(s: str, cap: int = MAX_OUTPUT_BYTES) -> str:
    if len(s) <= cap:
        return s
    return f"{s[: cap - 60]}\n\n[... output truncated at {cap} bytes ...]"


def _format_result(stdout: str, stderr: str, exit_code: int) -> str:
    out = stdout.rstrip()
    err = stderr.rstrip()
    parts: list[str] = []
    if out:
        parts.append(f"--- stdout ---\n{_truncate(out)}")
    if err:
        parts.append(f"--- stderr ---\n{_truncate(err)}")
    if exit_code != 0:
        parts.append(f"--- exit code ---\n{exit_code}")
    if not parts:
        return "(ts-code-mode) Program ran to completion with no output."
    return "\n\n".join(parts)


# ─── public entry point ─────────────────────────────────────────────────


async def execute_typescript_code(
    code: str,
    allowed_tools: Iterable[BaseTool],
    langchain_config: Optional[Dict[str, Any]] = None,
    deno_bin: Optional[str] = None,
) -> str:
    """Run the model's TS program in a locked-down Deno subprocess.

    ``allowed_tools`` is the *thread-described subset* of real tools —
    only these become callable shims on the ``codemode`` object inside
    the program. Names absent from this set aren't present at all, so a
    typo by the model fails at TS compile rather than RPC.
    """
    tools_list = list(allowed_tools)
    if not tools_list:
        return (
            "Error: no tools described yet. Call describe_tools(['name', ...]) "
            "first with the tools you want to use, then call execute_typescript."
        )

    deno_bin = deno_bin or os.getenv("DENO_BIN", "deno") or "deno"

    async with ToolRpcServer(tools_list, langchain_config) as rpc:
        tmp_dir = tempfile.mkdtemp(prefix="ts-code-mode-")
        try:
            codemode_ts = _generate_codemode_ts(tools_list, rpc.port, rpc.token)
            script_ts = _assemble_script(code)
            with open(os.path.join(tmp_dir, "codemode.ts"), "w", encoding="utf-8") as f:
                f.write(codemode_ts)
            with open(os.path.join(tmp_dir, "script.ts"), "w", encoding="utf-8") as f:
                f.write(script_ts)

            try:
                proc = await asyncio.create_subprocess_exec(
                    deno_bin,
                    "run",
                    "--no-prompt",
                    "--quiet",
                    f"--allow-net=127.0.0.1:{rpc.port}",
                    "script.ts",
                    cwd=tmp_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    # Strip parent env — the program shouldn't see our
                    # API keys or DB URL.
                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                )
            except FileNotFoundError:
                return (
                    f"Error: Deno binary not found at {deno_bin!r}. "
                    f"ts_code_mode needs Deno installed. On macOS: "
                    f"`brew install deno`. Override path with DENO_BIN env var."
                )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=WALL_CLOCK_SECONDS + GRACE_SECONDS,
                )
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.communicate(), timeout=2)
                except asyncio.TimeoutError:
                    pass
                return (
                    f"Error: ts_code_mode program timed out after "
                    f"{WALL_CLOCK_SECONDS + GRACE_SECONDS}s. Break the work "
                    f"into smaller programs or print less."
                )

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            return _format_result(stdout, stderr, proc.returncode or 0)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
