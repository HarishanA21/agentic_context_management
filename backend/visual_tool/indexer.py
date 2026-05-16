"""URL / citation / hash extractor — the "Index" in Hybrid Index Solution.

The point of this module: vision-language models hallucinate dense
alphanumeric data when reading rasterised text (URLs become wrong,
author names get misspelled, hex hashes flip digits). The paper's
breakthrough was to **bypass OCR entirely** for these fragile values
by extracting them at format time into a small raw-text block sent
alongside the image. The model is then instructed to cite verbatim
from that block.

This module is the extractor: given any formatted tool output, it
picks out the patterns the model would otherwise mis-OCR.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Tuple

# Patterns are tuned to err on the side of "include too much" — false
# positives in the REFERENCES block are harmless; false negatives mean
# the model has to OCR a value from the image.

# URLs — http/https only (skip schemes the model can't usefully cite).
_URL_RE = re.compile(r"https?://[^\s<>\"')\]}]+", re.I)

# Academic / journalistic citations: "Author et al. 2024" / "Author 2025".
_CITATION_RE = re.compile(
    r"\b[A-Z][A-Za-z'\-]+(?:\s+et\s+al\.?)?\s*[,(]?\s*\d{4}\b"
)

# Long alphanumeric runs likely to be IDs / hashes / tokens.
# Min 16 chars + must contain at least one digit and one letter to
# avoid grabbing all-caps acronyms or pure numbers (years, page counts).
_HASH_RE = re.compile(r"\b[A-Za-z0-9_\-]{16,}\b")

# File paths with recognised extensions — useful for line-cite style.
_FILE_PATH_RE = re.compile(
    r"(?:^|\s)([/\w.\-]+\.(?:py|ts|tsx|js|jsx|md|txt|json|yaml|yml|toml|html|css|sql))",
    re.M,
)

# Paper-style anchors: "[Doc 3]", "[Source 12]", etc.
_DOC_REF_RE = re.compile(r"\[(?:Doc|Source|Ref|Cite)\s*\d+\]", re.I)


def _is_balanced_id(value: str) -> bool:
    """For _HASH_RE matches: require ≥1 digit and ≥1 letter so we don't
    grab plain integers (years, scores) or plain acronyms."""
    has_digit = any(c.isdigit() for c in value)
    has_letter = any(c.isalpha() for c in value)
    return has_digit and has_letter


def extract_references(text: str) -> List[Tuple[str, str]]:
    """Return a deduplicated, order-preserving list of (label, value)
    tuples for every fragile pattern found in ``text``."""
    if not text:
        return []
    refs: List[Tuple[str, str]] = []
    seen: set[str] = set()

    def _push(label: str, value: str) -> None:
        # Trim trailing punctuation that often clings to URLs / hashes
        # ("see https://x.com, …"). Preserve [..] markers — those are
        # the whole point of doc refs.
        v = value.strip().rstrip(".,;:)}")
        if not v or v in seen:
            return
        seen.add(v)
        refs.append((label, v))

    # Order matters: URLs first because they're the most failure-prone
    # for OCR (the model loves to flip slashes / drop subdomains).
    for m in _URL_RE.finditer(text):
        _push("URL", m.group(0))
    for m in _DOC_REF_RE.finditer(text):
        _push("DOC", m.group(0))
    for m in _CITATION_RE.finditer(text):
        _push("CITE", m.group(0))
    for m in _FILE_PATH_RE.finditer(text):
        _push("FILE", m.group(1))
    for m in _HASH_RE.finditer(text):
        v = m.group(0)
        if _is_balanced_id(v):
            _push("ID", v)
    return refs


def build_references_block(refs: Iterable[Tuple[str, str]], *, header: str | None = None) -> str:
    """Render the REFERENCES list into a compact text block the model
    can copy from. Returns an empty string when there are no refs so
    the caller can decide whether to send a text part at all."""
    items = list(refs)
    if not items:
        return ""
    lines = [
        header
        or "REFERENCES (cite these verbatim — do NOT invent or modify):"
    ]
    for label, value in items:
        lines.append(f"  [{label}] {value}")
    return "\n".join(lines)


def merge_refs(
    *ref_lists: Iterable[Tuple[str, str]],
) -> List[Tuple[str, str]]:
    """Combine multiple ref lists, dedup by value, preserve first-seen
    order. Used when a per-tool template already emits some refs and
    the indexer wants to add what it found scanning the formatted text."""
    seen: set[str] = set()
    out: List[Tuple[str, str]] = []
    for lst in ref_lists:
        for label, value in lst:
            v = value.strip().rstrip(".,;:)}")
            if not v or v in seen:
                continue
            seen.add(v)
            out.append((label, v))
    return out
