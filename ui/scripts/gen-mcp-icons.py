#!/usr/bin/env python3
"""Regenerate ui/components/mcp-brand-icons.ts from simple-icons SVGs.

Run from the `ui/` directory:
    python3 scripts/gen-mcp-icons.py

Adding a new brand-icon? Append to `MAPPING` below using the simple-icons
slug (search at https://simpleicons.org) and rerun. Catalog slugs without
a brand entry fall back to a Lucide-style generic icon defined in
components/mcp-icons.tsx.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# our catalog slug → simple-icons slug
MAPPING: dict[str, str] = {
    "github":      "github",
    "postgres":    "postgresql",
    "sqlite":      "sqlite",
    "git":         "git",
    "notion":      "notion",
    "linear":      "linear",
    "cloudflare":  "cloudflare",
    "sentry":      "sentry",
    "stripe":      "stripe",
    "atlassian":   "atlassian",
    "huggingface": "huggingface",
    "brave":       "brave",
    "puppeteer":   "puppeteer",
    "redis":       "redis",
}

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "node_modules/simple-icons/icons"
META = ROOT / "node_modules/simple-icons/data/simple-icons.json"


def main() -> None:
    if not ICONS.exists():
        raise SystemExit("simple-icons not installed; run `npm install simple-icons`")
    metadata = json.loads(META.read_text())
    by_slug = {
        m.get("slug") or re.sub(r"\W", "", m["title"].lower()): m
        for m in metadata
    }

    out: list[tuple[str, str, str, str]] = []
    for our_key, si_slug in MAPPING.items():
        svg = ICONS / f"{si_slug}.svg"
        if not svg.exists():
            print(f"  missing: {si_slug}")
            continue
        path_match = re.search(r'<path d="([^"]+)"', svg.read_text())
        if not path_match:
            print(f"  no path: {si_slug}")
            continue
        info = by_slug.get(si_slug, {})
        out.append(
            (our_key, si_slug, info.get("hex", "888888"), path_match.group(1))
        )

    lines = [
        "// Auto-generated brand icons from simple-icons (CC0).",
        "// Regenerate with: python3 scripts/gen-mcp-icons.py",
        "",
        "export type BrandIcon = { path: string; color: string; title: string }",
        "",
        "export const BRAND_ICONS: Record<string, BrandIcon> = {",
    ]
    for k, s, h, p in sorted(out):
        lines.append(f'  "{k}": {{ color: "#{h}", title: "{s.title()}", path: "{p}" }},')
    lines.append("}")
    target = ROOT / "components/mcp-brand-icons.ts"
    target.parent.mkdir(exist_ok=True)
    target.write_text("\n".join(lines) + "\n")
    print(f"Wrote {target.relative_to(ROOT)} with {len(out)} icons.")


if __name__ == "__main__":
    main()
