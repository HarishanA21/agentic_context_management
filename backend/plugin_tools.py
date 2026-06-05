"""Plugin tool implementations.

A "plugin" (see plugins_catalog.py) is a small, real capability the user can add
to the agent from the Plugins directory. Each plugin maps to one or more of the
``@tool`` functions below; when a plugin is enabled, ``_get_agent_for_request``
adds its tools to the agent's toolbox, so the agent can actually call them.

These tools are dependency-light and need no API keys, so they work out of the
box. Add a new plugin by writing a tool here and registering it in
plugins_catalog.py.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone

import requests
from langchain.tools import tool


# ── web-fetch ────────────────────────────────────────────────────────────────
@tool
def fetch_url(url: str, max_chars: int = 4000) -> str:
    """Fetch a web page or HTTP API and return its text content.

    Use this to read a URL the user gives you, look something up online, or
    pull data from a public API. HTML is stripped to readable text.

    Args:
        url: The URL to fetch (http/https; a bare domain is assumed https).
        max_chars: Maximum characters of content to return (default 4000).
    """
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        resp = requests.get(
            url, timeout=15, headers={"User-Agent": "acm-agent/1.0"}
        )
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "").lower()
        text = resp.text
        if "html" in ctype or text.lstrip().lower().startswith("<!doctype html"):
            m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
            title = m.group(1).strip() if m else ""
            body = re.sub(
                r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.I | re.S
            )
            body = re.sub(r"<[^>]+>", " ", body)
            body = re.sub(r"\s+", " ", body).strip()
            out = (f"Title: {title}\n\n" if title else "") + body
        else:
            out = text
        clipped = out[:max_chars]
        if len(out) > max_chars:
            clipped += f"\n… [truncated, {len(out)} chars total]"
        return clipped or "(empty response)"
    except Exception as e:
        return f"Error fetching {url!r}: {e}"


# ── json-toolkit ─────────────────────────────────────────────────────────────
@tool
def json_tool(action: str, data: str) -> str:
    """Validate, pretty-print, or minify a JSON string.

    Args:
        action: One of 'validate', 'format' (pretty-print), or 'minify'.
        data: The JSON text to process.
    """
    try:
        obj = json.loads(data)
    except json.JSONDecodeError as e:
        return f"Invalid JSON: {e.msg} at line {e.lineno}, column {e.colno}."
    act = (action or "format").strip().lower()
    if act == "validate":
        kind = type(obj).__name__
        return f"Valid JSON (top-level type: {kind})."
    if act == "minify":
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    # default: format
    return json.dumps(obj, indent=2, ensure_ascii=False)


# ── unit-converter ───────────────────────────────────────────────────────────
# Each unit maps to (category, factor-to-base). Conversion within a category is
# value * from_factor / to_factor. Temperature is handled separately because it
# is affine, not linear.
_UNITS = {
    # length (base: metre)
    "mm": ("length", 0.001), "cm": ("length", 0.01), "m": ("length", 1.0),
    "km": ("length", 1000.0), "in": ("length", 0.0254), "ft": ("length", 0.3048),
    "yd": ("length", 0.9144), "mi": ("length", 1609.344),
    # mass (base: gram)
    "mg": ("mass", 0.001), "g": ("mass", 1.0), "kg": ("mass", 1000.0),
    "oz": ("mass", 28.349523125), "lb": ("mass", 453.59237),
    # data (base: byte)
    "b": ("data", 1.0), "kb": ("data", 1024.0), "mb": ("data", 1024.0 ** 2),
    "gb": ("data", 1024.0 ** 3), "tb": ("data", 1024.0 ** 4),
    # time (base: second)
    "s": ("time", 1.0), "sec": ("time", 1.0), "min": ("time", 60.0),
    "h": ("time", 3600.0), "hr": ("time", 3600.0), "day": ("time", 86400.0),
}
_TEMP = {"c", "celsius", "f", "fahrenheit", "k", "kelvin"}


def _to_celsius(v: float, u: str) -> float:
    if u in ("c", "celsius"):
        return v
    if u in ("f", "fahrenheit"):
        return (v - 32) * 5 / 9
    return v - 273.15  # kelvin


def _from_celsius(c: float, u: str) -> float:
    if u in ("c", "celsius"):
        return c
    if u in ("f", "fahrenheit"):
        return c * 9 / 5 + 32
    return c + 273.15  # kelvin


@tool
def convert_units(value: float, from_unit: str, to_unit: str) -> str:
    """Convert a value between common units.

    Supports length (mm, cm, m, km, in, ft, yd, mi), mass (mg, g, kg, oz, lb),
    data (b, kb, mb, gb, tb), time (s, min, h, day), and temperature
    (c/celsius, f/fahrenheit, k/kelvin).

    Args:
        value: The numeric value to convert.
        from_unit: The unit to convert from.
        to_unit: The unit to convert to.
    """
    f = (from_unit or "").strip().lower()
    t = (to_unit or "").strip().lower()
    try:
        value = float(value)
    except (TypeError, ValueError):
        return f"'{value}' is not a number."

    if f in _TEMP and t in _TEMP:
        result = _from_celsius(_to_celsius(value, f), t)
        return f"{value} {from_unit} = {result:g} {to_unit}"

    if f in _UNITS and t in _UNITS:
        fcat, ffac = _UNITS[f]
        tcat, tfac = _UNITS[t]
        if fcat != tcat:
            return (
                f"Can't convert {from_unit} ({fcat}) to {to_unit} ({tcat}) — "
                f"different kinds of unit."
            )
        result = value * ffac / tfac
        return f"{value} {from_unit} = {result:g} {to_unit}"

    return (
        f"Unknown unit in '{from_unit}' → '{to_unit}'. Supported: length, mass, "
        f"data, time, temperature units."
    )


# ── text-tools ───────────────────────────────────────────────────────────────
@tool
def text_transform(action: str, text: str) -> str:
    """Transform a string: encode/decode, hash, change case, reverse, or count.

    Args:
        action: One of base64_encode, base64_decode, url_encode, url_decode,
            sha256, md5, upper, lower, title, reverse, count.
        text: The input text.
    """
    a = (action or "").strip().lower()
    try:
        if a == "base64_encode":
            return base64.b64encode(text.encode()).decode()
        if a == "base64_decode":
            return base64.b64decode(text.encode()).decode("utf-8", "replace")
        if a == "url_encode":
            return urllib.parse.quote(text)
        if a == "url_decode":
            return urllib.parse.unquote(text)
        if a == "sha256":
            return hashlib.sha256(text.encode()).hexdigest()
        if a == "md5":
            return hashlib.md5(text.encode()).hexdigest()
        if a == "upper":
            return text.upper()
        if a == "lower":
            return text.lower()
        if a == "title":
            return text.title()
        if a == "reverse":
            return text[::-1]
        if a == "count":
            lines = len(text.splitlines()) or (1 if text else 0)
            return f"{len(text)} chars, {len(text.split())} words, {lines} lines"
        return (
            f"Unknown action '{action}'. Supported: base64_encode, "
            f"base64_decode, url_encode, url_decode, sha256, md5, upper, lower, "
            f"title, reverse, count."
        )
    except Exception as e:
        return f"Error: {e}"


# ── uuid-generator ───────────────────────────────────────────────────────────
@tool
def generate_uuid(count: int = 1) -> str:
    """Generate one or more random UUID v4 strings.

    Args:
        count: How many UUIDs to generate (1-50).
    """
    try:
        n = max(1, min(int(count), 50))
    except (TypeError, ValueError):
        n = 1
    return "\n".join(str(uuid.uuid4()) for _ in range(n))


# ── datetime ─────────────────────────────────────────────────────────────────
@tool
def datetime_tool(action: str, value: str = "") -> str:
    """Date/time helper, in UTC.

    Args:
        action: One of:
            now — the current UTC time (ISO + unix);
            unix_to_iso — convert a unix timestamp in `value` to ISO UTC;
            iso_to_unix — convert an ISO datetime in `value` to a unix timestamp;
            add — add a duration to a base time. Pass `value` as
                "<base> <duration>" where base is 'now' or an ISO datetime and
                duration is like 2d, 3h, 30m, 45s (e.g. "now 2d").
        value: Input for the action (see above).
    """
    try:
        a = (action or "").strip().lower()
        if a == "now":
            now = datetime.now(timezone.utc)
            return f"{now.isoformat()}  (unix {int(now.timestamp())})"
        if a == "unix_to_iso":
            return datetime.fromtimestamp(
                float(value.strip()), timezone.utc
            ).isoformat()
        if a == "iso_to_unix":
            dt = datetime.fromisoformat(value.strip())
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return str(int(dt.timestamp()))
        if a == "add":
            parts = value.split()
            base = parts[0] if parts else "now"
            dur = parts[1] if len(parts) > 1 else ""
            dt = (
                datetime.now(timezone.utc)
                if base.lower() == "now"
                else datetime.fromisoformat(base)
            )
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            m = re.fullmatch(r"(-?\d+)([dhms])", dur.strip())
            if not m:
                return (
                    "Provide a duration as the second token: 2d, 3h, 30m, or "
                    "45s (e.g. value='now 2d')."
                )
            amt, unit = int(m.group(1)), m.group(2)
            delta = {
                "d": timedelta(days=amt),
                "h": timedelta(hours=amt),
                "m": timedelta(minutes=amt),
                "s": timedelta(seconds=amt),
            }[unit]
            return (dt + delta).isoformat()
        return (
            f"Unknown action '{action}'. Supported: now, unix_to_iso, "
            f"iso_to_unix, add."
        )
    except Exception as e:
        return f"Error: {e}"


# ── regex-tester ─────────────────────────────────────────────────────────────
@tool
def regex_test(pattern: str, text: str, ignore_case: bool = False) -> str:
    """Test a regular expression against text and return the matches.

    Args:
        pattern: The regex pattern (Python syntax).
        text: The text to search.
        ignore_case: Case-insensitive matching if true.
    """
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        return f"Invalid regex: {e}"
    matches = list(rx.finditer(text))
    if not matches:
        return "No matches."
    out = [f"{len(matches)} match(es):"]
    for i, m in enumerate(matches[:50], 1):
        groups = f"  groups={m.groups()}" if m.groups() else ""
        out.append(f"  {i}. '{m.group(0)}' at {m.start()}-{m.end()}{groups}")
    if len(matches) > 50:
        out.append(f"  … and {len(matches) - 50} more")
    return "\n".join(out)
