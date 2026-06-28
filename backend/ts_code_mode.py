"""TypeScript Code Mode — catalog + stub generation + naming.

This module is the *static* half of the ts_code_mode strategy: it
converts our LangChain tools into the artefacts that show up in the
prompt and in the on-demand `describe_tools` response. Runtime pieces
(RPC channel, Deno runner) live in their own modules.

Three things live here:

1. ``sanitise_tool_name(name)`` — same hyphen/dot rule as Cloudflare's
   SDK so MCP-style names like ``my-server.list-items`` become legal
   TypeScript identifiers (``my_server_list_items``). Idempotent; safe
   names pass through unchanged.

2. ``generate_catalog(tools)`` — render the prompt-resident catalog.
   One row per tool: ``  name  — one-line summary``. Cheap; ~30 tokens
   per tool. Always visible to the model.

3. ``generate_ts_subset(tools)`` — render the full TypeScript surface
   for an *arbitrary subset* of tools: input/output interfaces with
   JSDoc + a ``declare const codemode: { … }`` block. This is what
   ``describe_tools`` returns; the prompt itself never carries the
   full interfaces.

The split is the whole point of the design: prompt cost scales with
the catalog (~constant), not with whatever the user has enabled.
"""

from __future__ import annotations

import re
import typing
from typing import Any, Iterable, List

from langchain_core.tools import BaseTool


# ─── name sanitisation ──────────────────────────────────────────────────


_VALID_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INVALID_CHAR_RE = re.compile(r"[^A-Za-z0-9_]")


def sanitise_tool_name(name: str) -> str:
    """Convert any tool name into a legal TypeScript identifier.

    Mirrors what Cloudflare's @cloudflare/codemode SDK does:
    ``my-server.list-items`` → ``my_server_list_items``. Names that are
    already valid identifiers pass through unchanged. We leave the
    upstream display name alone in the catalog rendering (see
    ``generate_catalog``) so the model sees both forms once.
    """
    if not name:
        return "_"
    if _VALID_IDENT_RE.match(name):
        return name
    cleaned = _INVALID_CHAR_RE.sub("_", name)
    if cleaned and cleaned[0].isdigit():
        cleaned = "_" + cleaned
    # Collapse runs of underscores so `foo--bar` becomes `foo_bar`,
    # not `foo__bar`. Less noisy and matches what humans would expect.
    cleaned = re.sub(r"_{2,}", "_", cleaned).strip("_")
    return cleaned or "_"


# ─── catalog (prompt-resident) ───────────────────────────────────────────


_FIRST_SENTENCE_RE = re.compile(r"^(.+?[.!?])(?:\s|$)", re.S)


def _one_line_summary(description: str | None, max_len: int = 80) -> str:
    """Pull a short, single-line summary from a tool's description.

    Strategy: take the first sentence (up to . ! ?). Strip newlines.
    Truncate to ``max_len`` with an ellipsis if still too long. We
    deliberately don't try to parse Args/Returns blocks — the
    description's opening line is what tool authors usually polish.
    """
    if not description:
        return ""
    flat = " ".join(description.split())
    m = _FIRST_SENTENCE_RE.match(flat)
    text = (m.group(1) if m else flat).rstrip(".")
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def generate_catalog(tools: Iterable[BaseTool]) -> str:
    """Render the prompt-resident catalog: one row per tool.

    Format:
    ::

      list_project_files     — list files in the current project
      read_project_file      — read a file (text / PDF / DOCX / …)
      run_shell              — run a bash command in the workspace

    Names are sanitised (so the catalog matches what the model will
    actually type in ``describe_tools(...)`` and code). Width is
    padded to the longest name so it reads as a column.
    """
    rows: list[tuple[str, str]] = []
    for t in tools:
        safe = sanitise_tool_name(t.name)
        rows.append((safe, _one_line_summary(t.description)))
    if not rows:
        return "  (no tools available)"
    width = max(len(r[0]) for r in rows)
    width = min(width, 36)  # avoid runaway alignment with very long names
    out: list[str] = []
    for safe, summary in rows:
        if summary:
            out.append(f"  {safe.ljust(width)}  — {summary}")
        else:
            out.append(f"  {safe}")
    return "\n".join(out)


# ─── TypeScript stub generation ─────────────────────────────────────────


_PRIM_TS: dict[Any, str] = {
    str: "string",
    int: "number",
    float: "number",
    bool: "boolean",
    bytes: "string",  # we b64 on the wire; surface as string to the model
    type(None): "null",
}


def _ts_type(ann: Any) -> str:
    """Best-effort Python annotation → TypeScript type string.

    Handles primitives, ``Optional[T]`` / ``T | None``, ``list[T]``,
    ``dict[K, V]``, ``Literal[…]``, and ``Union[…]``. Falls back to
    ``unknown`` rather than guessing — better than a wrong type when
    the model is going to write code against this.
    """
    if ann is None or ann is type(None):
        return "null"
    if ann in _PRIM_TS:
        return _PRIM_TS[ann]
    origin = typing.get_origin(ann)
    args = typing.get_args(ann)
    if origin is None:
        # Untyped or class — bail to unknown.
        name = getattr(ann, "__name__", None)
        if name in {"Any", "object"}:
            return "unknown"
        return "unknown"
    # Union[...] including Optional[T]
    if origin is typing.Union:
        parts = [_ts_type(a) for a in args]
        # Optional[T] renders as `T | null` rather than swallowing the
        # null arm, so the LLM sees that null is possible.
        return " | ".join(dict.fromkeys(parts)) or "unknown"
    if origin in (list, typing.List, tuple, typing.Tuple, set, typing.Set):
        inner = _ts_type(args[0]) if args else "unknown"
        return f"{inner}[]"
    if origin in (dict, typing.Dict):
        k = _ts_type(args[0]) if args else "string"
        v = _ts_type(args[1]) if len(args) > 1 else "unknown"
        # Keys are strings on the wire (JSON).
        return f"Record<{k if k == 'string' else 'string'}, {v}>"
    if origin is typing.Literal:
        parts = []
        for a in args:
            if isinstance(a, str):
                parts.append(f'"{a}"')
            elif isinstance(a, bool):
                parts.append("true" if a else "false")
            elif isinstance(a, (int, float)):
                parts.append(str(a))
            else:
                parts.append("unknown")
        return " | ".join(parts) or "unknown"
    return "unknown"


def _pascal_case(s: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", s)
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def _input_interface(tool: BaseTool, safe_name: str) -> str:
    """Build `interface FooInput { … }` from the tool's args_schema."""
    iface_name = f"{_pascal_case(safe_name)}Input"
    schema = tool.args_schema
    fields: list[str] = []
    if schema is not None and hasattr(schema, "model_fields"):
        for fname, finfo in schema.model_fields.items():
            ann = finfo.annotation
            # RunnableConfig is auto-injected by LangChain — never expose it.
            ann_str = repr(ann)
            if "RunnableConfig" in ann_str:
                continue
            ts = _ts_type(ann)
            optional = "" if finfo.is_required() else "?"
            description = (finfo.description or "").strip()
            if description:
                fields.append(f"  /** {description} */\n  {fname}{optional}: {ts}")
            else:
                fields.append(f"  {fname}{optional}: {ts}")
    body = "\n".join(fields) if fields else ""
    if body:
        return f"interface {iface_name} {{\n{body}\n}}"
    return f"interface {iface_name} {{}}"


def _output_interface(safe_name: str) -> str:
    """Output is always ``{ text: string }``.

    Every LangChain tool we expose returns a string today (the agent
    receives ``str`` as the tool-message content). Wrapping it in
    ``{ text }`` keeps the door open for richer payloads later without
    breaking the model's mental model of the API now.
    """
    return f"interface {_pascal_case(safe_name)}Output {{ text: string }}"


def _codemode_entry(tool: BaseTool, safe_name: str) -> str:
    """Render the entry inside `declare const codemode: { ... }`."""
    summary = (tool.description or "").strip().splitlines()
    summary_first = summary[0] if summary else ""
    jsdoc = f"  /**\n   * {summary_first}\n   */" if summary_first else ""
    pascal = _pascal_case(safe_name)
    return (
        f"{jsdoc}\n"
        f"  {safe_name}: (input: {pascal}Input) => Promise<{pascal}Output>"
    ).lstrip("\n")


def generate_ts_subset(tools: Iterable[BaseTool]) -> str:
    """Render the full TS surface for an arbitrary subset of tools.

    Returns one block: every input/output interface, then a single
    ``declare const codemode: { … }`` block with one entry per tool.
    This is exactly what ``describe_tools`` hands back to the model
    when it asks about specific names. The prompt itself never carries
    this — only the catalog does.
    """
    tools_list = list(tools)
    if not tools_list:
        return "// no matching tools"
    interfaces: list[str] = []
    entries: list[str] = []
    for t in tools_list:
        safe = sanitise_tool_name(t.name)
        interfaces.append(_input_interface(t, safe))
        interfaces.append(_output_interface(safe))
        entries.append(_codemode_entry(t, safe))
    interfaces_block = "\n\n".join(interfaces)
    entries_block = "\n\n".join(entries)
    return (
        f"{interfaces_block}\n\n"
        f"declare const codemode: {{\n"
        f"{entries_block}\n"
        f"}}"
    )


# ─── system prompt ──────────────────────────────────────────────────────


TS_CODE_MODE_PROMPT_HEADER = """\
You are a helpful assistant for a coding project. You operate by writing
short TypeScript programs that call the project's tool API. Instead of
calling tools directly, you have TWO tools:

  * describe_tools(names: string[])   — fetch the TypeScript interfaces
                                        and JSDoc for the tools you plan
                                        to use this turn. Returns a code
                                        block you can copy types from.
  * execute_typescript(code: string)  — run a TypeScript program that
                                        uses `codemode.<name>(input)`.
                                        The stdout of the program is
                                        returned to you.

HOW TO WORK:
1. Pick the tools you need from the catalog below.
2. If you haven't already described them in this conversation, call
   describe_tools with their names. Names you've described are sticky
   — you do NOT need to describe them again in later turns.
3. Call execute_typescript with a single program that:
   - awaits whatever codemode.<name>(...) calls you need;
   - uses console.log(...) for every value you want returned;
   - is wrapped in your top-level code (no `import`, no `fetch`, no
     `node:*` / `deno:*` modules — only `codemode` is available).
4. After execute_typescript returns, summarise the result for the user
   in plain prose. Don't paste raw stdout unless they asked.
5. If the request is pure chat (no tools needed), reply directly.

CHAIN IN CODE, NOT TURNS:
  ✅ One execute_typescript call that loops over files and prints results.
  ❌ One execute_typescript per file, ten round trips with the model.

HARD LIMITS:
  - 30s wall clock per execute_typescript.
  - 16 KB combined stdout + stderr.
  - No filesystem, no network, no subprocess. Tool calls only.

CATALOG (call describe_tools to see full interfaces):

{catalog}
"""


def ts_code_mode_system_prompt(tools: Iterable[BaseTool]) -> str:
    return TS_CODE_MODE_PROMPT_HEADER.format(catalog=generate_catalog(tools))


# ─── lookup helpers ────────────────────────────────────────────────────


def build_name_index(tools: Iterable[BaseTool]) -> dict[str, BaseTool]:
    """Map both sanitised AND original names to the same BaseTool, so a
    model that uses either form when calling describe_tools / writing
    code resolves correctly. Sanitised name wins on collisions.
    """
    index: dict[str, BaseTool] = {}
    # Seed originals first so the sanitised pass overwrites on conflict.
    for t in tools:
        index[t.name] = t
    for t in tools:
        index[sanitise_tool_name(t.name)] = t
    return index


def resolve_names(
    requested: List[str], index: dict[str, BaseTool]
) -> tuple[List[BaseTool], List[str]]:
    """Look up requested tool names against ``index``.

    Returns ``(found_tools, unknown_names)``. Order of ``found_tools``
    matches the order of unique successful lookups in ``requested``;
    deduplicated by id() so we never describe the same tool twice in
    one response.
    """
    found: list[BaseTool] = []
    seen_ids: set[int] = set()
    unknown: list[str] = []
    for raw in requested:
        name = (raw or "").strip()
        if not name:
            continue
        tool = index.get(name) or index.get(sanitise_tool_name(name))
        if tool is None:
            unknown.append(name)
            continue
        if id(tool) in seen_ids:
            continue
        seen_ids.add(id(tool))
        found.append(tool)
    return found, unknown
