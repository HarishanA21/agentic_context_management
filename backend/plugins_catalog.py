"""Plugin catalog + tool registry.

A plugin is a real capability the user can add to the agent from the Plugins
directory. Each catalog entry maps to one or more ``@tool`` functions in
``plugin_tools``. When a plugin is enabled (per-user, tracked in the ``plugins``
table), ``_get_agent_for_request`` adds its tools to the agent's toolbox, so the
agent can actually call them.

Add a plugin by writing its tool(s) in plugin_tools.py and adding an entry here
plus a row in ``_TOOLS_BY_SLUG``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from plugin_tools import (
    convert_units,
    datetime_tool,
    fetch_url,
    generate_uuid,
    json_tool,
    list_directory,
    regex_test,
    review_python,
    system_info,
    text_transform,
)


# ── Catalog ──────────────────────────────────────────────────────────────────
# Each entry powers a card in the Plugins directory. Fields:
#   slug        — stable id (also the toggle key).
#   name        — card title.
#   publisher   — vendor label on the card.
#   description — card blurb + what the plugin does.
#   icon        — UI glyph hint ("web" | "code" | "ruler").
#   tools       — display list of the tool names the plugin adds.
CATALOG: List[Dict[str, Any]] = [
    {
        "slug": "web-fetch",
        "name": "Web Fetch",
        "publisher": "Built-in",
        "description": (
            "Let the agent fetch a web page or HTTP API and read its content. "
            "Adds a fetch_url tool — useful for looking things up, reading a "
            "link the user pastes, or pulling data from a public API."
        ),
        "icon": "web",
        "tools": ["fetch_url"],
    },
    {
        "slug": "json-toolkit",
        "name": "JSON Toolkit",
        "publisher": "Built-in",
        "description": (
            "Validate, pretty-print, and minify JSON. Adds a json_tool the agent "
            "can call to check or reformat JSON the user provides."
        ),
        "icon": "code",
        "tools": ["json_tool"],
    },
    {
        "slug": "unit-converter",
        "name": "Unit Converter",
        "publisher": "Built-in",
        "description": (
            "Convert between common units — length, mass, data, time, and "
            "temperature. Adds a convert_units tool for accurate conversions."
        ),
        "icon": "ruler",
        "tools": ["convert_units"],
    },
    {
        "slug": "text-tools",
        "name": "Text Tools",
        "publisher": "Built-in",
        "description": (
            "Encode/decode (base64, URL), hash (sha256, md5), change case, "
            "reverse, and count text. Adds a text_transform tool."
        ),
        "icon": "code",
        "tools": ["text_transform"],
    },
    {
        "slug": "uuid-generator",
        "name": "UUID Generator",
        "publisher": "Built-in",
        "description": (
            "Generate random UUID v4 identifiers. Adds a generate_uuid tool — "
            "useful for ids, keys, and test data."
        ),
        "icon": "hash",
        "tools": ["generate_uuid"],
    },
    {
        "slug": "datetime",
        "name": "Date & Time",
        "publisher": "Built-in",
        "description": (
            "Get the current UTC time, convert between unix timestamps and ISO, "
            "and do date math. Adds a datetime_tool the agent can call."
        ),
        "icon": "clock",
        "tools": ["datetime_tool"],
    },
    {
        "slug": "regex-tester",
        "name": "Regex Tester",
        "publisher": "Built-in",
        "description": (
            "Test a regular expression against text and see the matches and "
            "capture groups. Adds a regex_test tool."
        ),
        "icon": "code",
        "tools": ["regex_test"],
    },
    {
        "slug": "desktop-commander",
        "name": "Desktop Commander",
        "publisher": "Desktop Commander",
        "description": (
            "Inspect the machine the agent runs on — system info and read-only "
            "file/folder listing. Adds system_info and list_directory tools."
        ),
        "icon": "terminal",
        "tools": ["system_info", "list_directory"],
    },
    {
        "slug": "qodo-code-review",
        "name": "Code Review",
        "publisher": "Qodo.ai",
        "description": (
            "Shift-left code review — static analysis of a Python snippet for "
            "syntax, missing docstrings, and overly long functions. Adds a "
            "review_python tool."
        ),
        "icon": "code",
        "tools": ["review_python"],
    },
]

# slug -> the actual tool objects the plugin contributes.
_TOOLS_BY_SLUG: Dict[str, list] = {
    "web-fetch": [fetch_url],
    "json-toolkit": [json_tool],
    "unit-converter": [convert_units],
    "text-tools": [text_transform],
    "uuid-generator": [generate_uuid],
    "datetime": [datetime_tool],
    "regex-tester": [regex_test],
    "desktop-commander": [system_info, list_directory],
    "qodo-code-review": [review_python],
}

_CATALOG_BY_SLUG: Dict[str, Dict[str, Any]] = {e["slug"]: e for e in CATALOG}


def catalog_entry(slug: str) -> Optional[Dict[str, Any]]:
    return _CATALOG_BY_SLUG.get(slug)


def build_plugin_tools(slugs: List[str]) -> list:
    """Return the tool objects for the given enabled plugin slugs (deduped,
    order-stable). Unknown slugs are ignored."""
    out: list = []
    seen = set()
    for slug in slugs:
        for tool_obj in _TOOLS_BY_SLUG.get(slug, []):
            if tool_obj.name not in seen:
                seen.add(tool_obj.name)
                out.append(tool_obj)
    return out
