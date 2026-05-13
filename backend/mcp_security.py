"""Validation + safety guards for MCP configurations.

Two main jobs:

  1. Reject custom HTTP/SSE endpoints that point at private/localhost IPs
     or non-https schemes. Resolves the hostname at validation time AND
     leaves a hook (`recheck_url_before_connect`) the runtime can call
     right before opening a session to defeat DNS rebinding.

  2. Reject stdio commands outside an allowlist of safe entry binaries
     (`npx`, `uvx`, `python`, `node`) and scrub args for shell
     metacharacters that should never appear in a structured args list.

Catalog-shipped entries bypass these checks (their URLs are vetted in the
JSON file). User-defined custom entries go through them.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from typing import Iterable, Optional, Tuple
from urllib.parse import urlparse


# Allow http://localhost only when explicitly enabled — useful for
# developers running a local MCP server.
ALLOW_LOCAL_HTTP = os.environ.get("MCP_ALLOW_LOCAL_HTTP", "").lower() in {
    "1",
    "true",
    "yes",
}

# Public catalog endpoints we trust regardless of resolution result. Used
# only for catalog-derived rows; custom rows still get full IP checks
# even when their URL happens to match.
CATALOG_DOMAIN_ALLOWLIST: set[str] = {
    "mcp.linear.app",
    "mcp.notion.com",
    "mcp.stripe.com",
    "mcp.atlassian.com",
    "mcp.sentry.dev",
    "docs.mcp.cloudflare.com",
    "app.getoutline.com",
    "huggingface.co",
}

STDIO_COMMAND_ALLOWLIST: set[str] = {
    "npx", "uvx", "python", "python3", "node",
}

# Characters that have no business in a structured args list. The args
# array is passed directly to subprocess.Popen so no shell parses them,
# but rejecting these defends against accidental injection should the
# args ever be re-stringified (e.g. for logging) and re-parsed elsewhere.
_BAD_ARG_CHARS = re.compile(r"[;&|`$<>]")


class MCPValidationError(ValueError):
    """Raised when an MCP config fails validation. The message is safe
    to surface in the UI."""


def _is_private_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_all(hostname: str) -> Iterable[str]:
    """Resolve a hostname to all its A / AAAA records. We check every
    answer because a name can resolve to a public address now and a
    private one a moment later (DNS rebinding)."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip in seen:
            continue
        seen.add(ip)
        out.append(ip)
    return out


def validate_endpoint_url(
    url: str,
    *,
    allow_catalog_domain: bool = False,
) -> str:
    """Validate that `url` is a safe MCP endpoint.

    Returns the URL unchanged (caller stores whatever they passed in).
    Raises `MCPValidationError` on rejection.

    `allow_catalog_domain` is True for catalog-derived rows so users
    can't be locked out of their own published-MCP endpoints if those
    hosts happen to also resolve to a CDN edge in private space.
    """
    if not url or not isinstance(url, str):
        raise MCPValidationError("Endpoint URL is required.")
    try:
        parsed = urlparse(url)
    except Exception:
        raise MCPValidationError("Endpoint URL is not a valid URL.")
    if parsed.scheme not in ("https", "http"):
        raise MCPValidationError("Endpoint URL must start with https:// or http://.")
    host = (parsed.hostname or "").strip()
    if not host:
        raise MCPValidationError("Endpoint URL is missing a hostname.")
    is_localhost = host in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "http":
        if not (is_localhost and ALLOW_LOCAL_HTTP):
            raise MCPValidationError(
                "Only https:// URLs are allowed (set MCP_ALLOW_LOCAL_HTTP=1 "
                "for http://localhost in dev)."
            )
    if allow_catalog_domain and host.lower() in CATALOG_DOMAIN_ALLOWLIST:
        return url
    if is_localhost and not ALLOW_LOCAL_HTTP:
        raise MCPValidationError(
            "Localhost endpoints are blocked (set MCP_ALLOW_LOCAL_HTTP=1 in "
            "the backend env to allow them in dev)."
        )
    # Hostname → IPs. Reject if any resolved address is private.
    for ip in _resolve_all(host):
        if _is_private_ip(ip):
            # localhost branch already handled above; we get here only for
            # non-localhost names that resolve to private space (e.g. an
            # internal DNS entry pointing at 10.x).
            if ALLOW_LOCAL_HTTP and is_localhost:
                continue
            raise MCPValidationError(
                f"Endpoint resolves to a private/internal IP ({ip}); only "
                "public endpoints are allowed."
            )
    return url


def recheck_url_before_connect(url: str, *, allow_catalog_domain: bool) -> None:
    """Cheap re-resolution right before opening a connection. Defeats DNS
    rebinding: at this point the row's URL was validated at save time,
    but the name could have flipped to a private IP since."""
    try:
        validate_endpoint_url(url, allow_catalog_domain=allow_catalog_domain)
    except MCPValidationError:
        raise
    except Exception as e:
        raise MCPValidationError(f"Pre-connect URL check failed: {e}")


def validate_stdio(command: str, args: list[str]) -> Tuple[str, list[str]]:
    """Validate an stdio command + args. Returns the (possibly normalised)
    pair. Raises `MCPValidationError` on rejection."""
    if not command or not isinstance(command, str):
        raise MCPValidationError("stdio command is required.")
    # Reject absolute paths — only let users pick from the allowlist.
    if "/" in command:
        raise MCPValidationError(
            f"Command must be one of: {sorted(STDIO_COMMAND_ALLOWLIST)}"
        )
    if command not in STDIO_COMMAND_ALLOWLIST:
        raise MCPValidationError(
            f"Command {command!r} is not allowed. Use one of: "
            f"{sorted(STDIO_COMMAND_ALLOWLIST)}"
        )
    if not isinstance(args, list):
        raise MCPValidationError("Arguments must be an array of strings.")
    cleaned: list[str] = []
    for a in args:
        if not isinstance(a, str):
            raise MCPValidationError("Every argument must be a string.")
        if _BAD_ARG_CHARS.search(a):
            raise MCPValidationError(
                f"Argument {a!r} contains a forbidden character "
                f"({_BAD_ARG_CHARS.pattern})."
            )
        cleaned.append(a)
    return command, cleaned


# Per-user concurrency cap. We don't strictly need the value at request
# time — the create endpoint enforces it — but a small helper keeps the
# meaning in one place.
MAX_MCPS_PER_USER = int(os.environ.get("MCP_MAX_PER_USER", "20"))
