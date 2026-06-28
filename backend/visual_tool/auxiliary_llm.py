"""Auxiliary LLM Template Generator — the paper's R2 mechanism.

Column 2 of the demo (Normal Tool Call + Image) sees tools whose
output shape we don't know in advance (any of our built-in tools or
any enabled MCP tool). For each new ``tool_name`` we encounter, we:

  1. Ship a sample of its output to a small / fast LLM (default
     ``google/gemini-2.5-flash`` via OpenRouter — the paper's
     efficiency winner).
  2. Ask the LLM to write a Python ``format(output) -> (text, refs)``
     function tailored to that shape.
  3. Validate and ``exec`` the function in a restricted namespace
     (only ``json`` + ``re`` available, no imports, no I/O).
  4. Cache the callable keyed by tool name.

Every later call to the same tool reuses the cached function — **zero
LLM cost** after warm-up. This is the "compile once, render many"
shape the paper benchmarks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from langchain_core.messages import HumanMessage, SystemMessage

from .indexer import extract_references, merge_refs


log = logging.getLogger(__name__)


# A formatter function: takes raw tool output, returns (formatted_text,
# list of (label, value) reference tuples).
Formatter = Callable[[str], Tuple[str, List[Tuple[str, str]]]]


# ─── prompt + parsing ──────────────────────────────────────────────────


_SYSTEM_PROMPT = (
    "You are a Just-in-Time template generator for an AI agent. "
    "Given a sample of one tool's output, you write a Python "
    "function that formats that tool's output into a clean, dense, "
    "rasterisable text suitable for a vision-language model to read. "
    "Your function will be cached and reused for every subsequent "
    "call to that tool — so make it general (handle the shape, not "
    "the specific values in the sample)."
)


_USER_PROMPT_TEMPLATE = """\
Tool name: {tool_name}

Sample output (truncated to 1500 chars):
<<<SAMPLE>>>
{sample}
<<<END_SAMPLE>>>

Write a Python function with this EXACT signature:

    def format(output: str) -> tuple[str, list[tuple[str, str]]]:
        ...

Behaviour:
- `output` is the raw tool return as a string (often JSON or formatted text).
- Return two things:
    1. `formatted_text`: a clean readable string. Strip JSON syntax
       noise (`{{`, `}}`, quoted keys). Use headings, indentation, blank
       lines. Make the spatial structure visible. Be terse but complete.
    2. `refs`: a list of `(label, value)` tuples for any URLs, IDs,
       hashes, file paths, or citations the LLM should never misread.
       Use labels like "URL", "FILE", "ID", "CITE", "TOOL".

Constraints (your code MUST follow these):
- ONLY use these names from the standard library: `json`, `re`.
  (They are already imported. No other imports allowed.)
- No file I/O, no network calls, no `open`, no `eval`, no `exec`.
- Function must be deterministic and side-effect-free.
- Handle malformed input gracefully — wrap the JSON parse in try/except
  and fall back to returning the raw string.

Output ONLY the Python code starting with `def format(`. Do NOT wrap it
in markdown code fences. Do NOT add explanations before or after.
"""


_CODE_FENCE_RE = re.compile(r"^```(?:python)?\s*\n|\n```\s*$", re.M)


def _strip_fences(code: str) -> str:
    """Defensive: some models add ```python ... ``` fences despite
    being told not to."""
    out = _CODE_FENCE_RE.sub("", code).strip()
    # Trim leading prose if the model ignored the instruction.
    if not out.lstrip().startswith("def "):
        idx = out.find("def format(")
        if idx >= 0:
            out = out[idx:]
    return out


# ─── exec sandbox ──────────────────────────────────────────────────────


_SAFE_BUILTINS = {
    "abs", "all", "any", "bool", "bytes", "callable", "chr", "dict",
    "divmod", "enumerate", "filter", "float", "format", "frozenset",
    "getattr", "hasattr", "hash", "hex", "int", "isinstance",
    "issubclass", "iter", "len", "list", "map", "max", "min", "next",
    "object", "oct", "ord", "pow", "range", "repr", "reversed",
    "round", "set", "slice", "sorted", "str", "sum", "tuple", "type",
    "vars", "zip",
    # exceptions a defensive formatter may want to catch
    "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
    "AttributeError", "RuntimeError",
    # constants
    "True", "False", "None",
}


def _build_exec_globals() -> Dict[str, Any]:
    import builtins as _builtins

    restricted: Dict[str, Any] = {
        n: getattr(_builtins, n)
        for n in _SAFE_BUILTINS
        if hasattr(_builtins, n)
    }
    # Block all imports — including __import__ — so generated code
    # cannot reach for os / subprocess / network.
    def _blocked_import(*_a, **_k):
        raise ImportError("imports are not allowed in generated templates")

    restricted["__import__"] = _blocked_import
    return {
        "__builtins__": restricted,
        # Pre-import the only two modules the prompt allows.
        "json": json,
        "re": re,
    }


def _compile_formatter(code: str) -> Formatter:
    """exec the generated code in the restricted globals and return the
    `format` callable. Raises on any compile / validation error."""
    code = _strip_fences(code)
    if not code or "def format" not in code:
        raise ValueError("generated code does not define a `format` function")
    compiled = compile(code, "<aux_template>", "exec")
    ns: Dict[str, Any] = _build_exec_globals()
    exec(compiled, ns)  # noqa: S102 — restricted sandbox above
    fn = ns.get("format")
    if not callable(fn):
        raise ValueError("`format` is not callable")
    return fn  # type: ignore[return-value]


def _safe_call_formatter(fn: Formatter, output: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Invoke the generated function defensively. If it raises or
    returns something malformed, fall back to a plain pass-through so
    the demo never hard-fails."""
    try:
        result = fn(output)
    except Exception as e:
        log.warning("[visual_tool] generated formatter raised %r — falling back", e)
        return output, extract_references(output)
    # Validate shape
    if (
        not isinstance(result, tuple)
        or len(result) != 2
        or not isinstance(result[0], str)
        or not isinstance(result[1], list)
    ):
        log.warning("[visual_tool] generated formatter returned wrong shape — falling back")
        return output, extract_references(output)
    formatted, refs = result
    # Normalise refs to (str, str) pairs and merge with what the
    # indexer can pick up (belt + braces against missed URLs).
    norm_refs: List[Tuple[str, str]] = []
    for item in refs:
        try:
            label, value = item
            if isinstance(label, str) and isinstance(value, str) and value:
                norm_refs.append((label, value))
        except (TypeError, ValueError):
            continue
    return formatted, merge_refs(norm_refs, extract_references(formatted))


# ─── cache ─────────────────────────────────────────────────────────────


_CACHE: Dict[str, Formatter] = {}
_CACHE_LOCK = threading.Lock()
# Tools the LLM call failed for — fall back to identity to avoid
# retrying forever inside a single demo run.
_FAILED: Dict[str, str] = {}


def _identity_formatter(output: str) -> Tuple[str, List[Tuple[str, str]]]:
    return output, extract_references(output)


# ─── LLM call ──────────────────────────────────────────────────────────


def _default_model_slug() -> str:
    return (
        os.getenv("AUXILIARY_TEMPLATE_MODEL")
        or os.getenv("VISUAL_TOOL_TEMPLATE_MODEL")
        or "google/gemini-2.5-flash"
    )


def _build_template_model():
    """Build the small LLM used for template generation. Re-uses the
    main `_build_model` helper so it goes through OpenRouter with the
    same auth/billing as everything else."""
    from api import _build_model  # lazy to avoid circular import

    return _build_model(_default_model_slug())


async def _generate_template_async(tool_name: str, sample: str) -> Optional[Formatter]:
    """Async — calls the LLM once. Returns None on any failure (cache
    a NOOP so we don't retry)."""
    sample_snippet = (sample or "")[:1500]
    user_msg = _USER_PROMPT_TEMPLATE.format(
        tool_name=tool_name, sample=sample_snippet
    )
    try:
        model = _build_template_model()
        resp = await model.ainvoke(
            [
                SystemMessage(content=_SYSTEM_PROMPT),
                HumanMessage(content=user_msg),
            ]
        )
        code = resp.content if isinstance(resp.content, str) else str(resp.content)
        return _compile_formatter(code)
    except Exception as e:
        log.warning(
            "[visual_tool] aux template generation failed for %r: %r",
            tool_name, e,
        )
        return None


async def get_formatter_async(tool_name: str, sample: str) -> Formatter:
    """Public async entry. Returns a cached formatter or generates +
    caches a new one. Never raises — falls back to identity on any
    generation failure so the demo run completes."""
    with _CACHE_LOCK:
        cached = _CACHE.get(tool_name)
        if cached is not None:
            return cached
        if tool_name in _FAILED:
            return _identity_formatter

    fn = await _generate_template_async(tool_name, sample)
    if fn is None:
        with _CACHE_LOCK:
            _FAILED[tool_name] = "generation failed"
        return _identity_formatter

    with _CACHE_LOCK:
        _CACHE[tool_name] = fn
    return fn


def format_via_aux_sync(tool_name: str, output: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Sync wrapper. Handles the "we're inside an event loop already"
    case by running the async path in a fresh loop on a worker thread."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — straight asyncio.run is fine.
        fn = asyncio.run(get_formatter_async(tool_name, output))
        return _safe_call_formatter(fn, output)
    # We're already in an event loop (e.g. /demo/compare). Hop to a
    # worker thread to spin up a nested loop.
    import concurrent.futures

    def _runner():
        return asyncio.run(get_formatter_async(tool_name, output))

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fn = ex.submit(_runner).result(timeout=30)
    return _safe_call_formatter(fn, output)


async def format_via_aux_async(
    tool_name: str, output: str
) -> Tuple[str, List[Tuple[str, str]]]:
    """Async-friendly entry point — use this from async call sites."""
    fn = await get_formatter_async(tool_name, output)
    return _safe_call_formatter(fn, output)


# ─── test helpers ──────────────────────────────────────────────────────


def _reset_cache_for_tests() -> None:
    """Called by unit tests to start fresh."""
    with _CACHE_LOCK:
        _CACHE.clear()
        _FAILED.clear()
