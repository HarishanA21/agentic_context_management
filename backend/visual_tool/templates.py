"""Hand-written formatters for Column 4 (Code Mode + Image).

Code Mode has exactly two top-level LangChain tools — ``describe_tools``
and ``execute_typescript`` — so we ship purpose-built formatters
instead of paying an auxiliary LLM call.

Each formatter takes the raw tool-return string and returns
``(formatted_text, refs)`` where:
  * ``formatted_text`` is the body that will be rasterised to PNG.
  * ``refs`` is a list of ``(label, value)`` pairs for the REFERENCES
    text block (anti-OCR-hallucination index).

The compressor stitches both into a multimodal ToolMessage.
"""

from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple

from .indexer import extract_references, merge_refs


# ─── describe_tools ─────────────────────────────────────────────────────


_TS_FUNC_RE = re.compile(
    r"(?P<name>\w+)\s*:\s*\(\s*input\s*:\s*(?P<arg>\w+)\s*\)",
)


def format_describe_tools(output: str) -> Tuple[str, List[Tuple[str, str]]]:
    """describe_tools already emits TypeScript-style interface blocks.
    We pass it through largely intact (it's already optimised for human
    + LLM reading) and extract tool / interface names as refs so the
    parent agent never mis-spells one when it later writes a program.
    """
    body = (output or "").strip()
    if not body:
        body = "(describe_tools returned nothing)"

    refs: List[Tuple[str, str]] = []
    # Tool entry points: `read_project_file: (input: ReadProjectFileInput)`
    for m in _TS_FUNC_RE.finditer(body):
        refs.append(("TOOL", m.group("name")))
    # Pick up `interface FooInput`/`interface BarOutput` names too.
    for m in re.finditer(r"\binterface\s+(\w+)\b", body):
        refs.append(("TYPE", m.group(1)))

    # Add anything else the generic indexer notices (file paths etc.
    # often show up in JSDoc strings).
    refs = merge_refs(refs, extract_references(body))

    header = "// === describe_tools result ==="
    formatted = f"{header}\n\n{body}"
    return formatted, refs


# ─── execute_typescript ────────────────────────────────────────────────


def format_execute_typescript(output: str) -> Tuple[str, List[Tuple[str, str]]]:
    """The runner returns ``--- stdout --- … --- stderr --- … --- exit code ---``
    style blocks. We straight-pipe it (already clean) but stamp section
    badges so a vision model can scan the spatial layout fast.
    """
    body = (output or "").strip()
    if not body:
        body = "(execute_typescript returned nothing)"

    # Make the section markers visually distinctive — easier for the
    # vision encoder to segment.
    body = re.sub(r"^--- stdout ---$", "===== STDOUT =====", body, flags=re.M)
    body = re.sub(r"^--- stderr ---$", "===== STDERR =====", body, flags=re.M)
    body = re.sub(r"^--- exit code ---$", "===== EXIT =====", body, flags=re.M)
    body = re.sub(r"^--- exception ---$", "===== EXCEPTION =====", body, flags=re.M)

    # Pull all the OCR-fragile values out of the body for the REFERENCES
    # block. Tool stdout often carries URLs, file paths, error tokens.
    refs = extract_references(body)
    return body, refs


# ─── registry ──────────────────────────────────────────────────────────


_REGISTRY: dict[str, Callable[[str], Tuple[str, List[Tuple[str, str]]]]] = {
    "describe_tools": format_describe_tools,
    "execute_typescript": format_execute_typescript,
}


def format_for_tool(
    tool_name: str, output: str
) -> Optional[Tuple[str, List[Tuple[str, str]]]]:
    """Look up a predefined template by tool name. Returns None if no
    hand-written template exists — caller can fall back to the
    auxiliary-LLM path."""
    fmt = _REGISTRY.get(tool_name)
    if fmt is None:
        return None
    return fmt(output)


def has_template(tool_name: str) -> bool:
    return tool_name in _REGISTRY
