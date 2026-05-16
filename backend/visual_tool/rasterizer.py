"""Render formatted text to a 2-column 1024 px PNG via Pillow.

Implements the paper's "Rust Optimised Image Formatting Engine" pattern
in pure Python (Pillow). Differences from the paper:
  - Pillow, not Rust + imageproc — easier to ship, ~150 ms per render
    vs the paper's ~25 ms. Acceptable for the demo path. Phase C in the
    plan upgrades to Rust if speed becomes a blocker.
  - 1024 px canvas width with a fixed 2-column layout (the paper's
    Pareto-winning geometry).
  - Monospace font (DejaVu Sans Mono / Menlo). High contrast (black
    on white). The paper proves this layout maximises vision-encoder
    accuracy per pixel.

Public entry point:
    >>> from visual_tool.rasterizer import render_2col
    >>> png_bytes = render_2col("formatted body text\\n…")
"""

from __future__ import annotations

import io
import logging
import os
from typing import Optional

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "Pillow is required for visual_tool.rasterizer — "
        "install with `pip install Pillow`"
    ) from e


log = logging.getLogger(__name__)


# ── geometry (matches the paper's image_format_2col_index layout) ────────
CANVAS_WIDTH = 1024
MARGIN = 16
COLUMN_GAP = 24
DEFAULT_FONT_SIZE = 12
LINE_HEIGHT = 16        # 12 pt + ~30 % leading
MAX_CANVAS_HEIGHT = 8192  # safety cap


# ── font discovery ──────────────────────────────────────────────────────


_FONT_CACHE: dict[int, "ImageFont.FreeTypeFont | ImageFont.ImageFont"] = {}


_MONO_CANDIDATES = [
    # macOS
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/Library/Fonts/Andale Mono.ttf",
    # Linux (Debian/Ubuntu)
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    # Common dev box install paths
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/usr/local/share/fonts/DejaVuSansMono.ttf",
]


def _find_mono_font(size: int) -> "ImageFont.FreeTypeFont | ImageFont.ImageFont":
    cached = _FONT_CACHE.get(size)
    if cached is not None:
        return cached
    # Allow override via env (handy on weird CI images).
    override = os.getenv("VISUAL_TOOL_FONT_PATH")
    candidates = ([override] if override else []) + _MONO_CANDIDATES
    for path in candidates:
        if not path:
            continue
        try:
            font = ImageFont.truetype(path, size)
            _FONT_CACHE[size] = font
            return font
        except (OSError, IOError):
            continue
    # Fall back to PIL's built-in bitmap font. The vision-encoder
    # accuracy will be lower but renders still work.
    log.warning(
        "[visual_tool] no monospace TTF found; falling back to PIL default font"
    )
    font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


def _char_width(font) -> float:
    """Best-effort fixed-width measurement of one character."""
    try:
        # PIL ≥ 9.2
        return font.getlength("X")
    except AttributeError:
        # Older PIL
        try:
            bbox = font.getbbox("X")
            return float(bbox[2] - bbox[0])
        except Exception:
            return 7.0  # rough fallback for the default bitmap font


def _wrap_text_hard(text: str, max_chars: int) -> list[str]:
    """Wrap long lines by character count (we want strict pixel-width
    boundaries; word wrap would leave ragged edges that hurt density)."""
    out: list[str] = []
    if max_chars < 4:
        max_chars = 4
    for line in text.splitlines():
        if not line:
            out.append("")
            continue
        while len(line) > max_chars:
            out.append(line[:max_chars])
            line = line[max_chars:]
        out.append(line)
    return out


def render_2col(
    text: str,
    *,
    width: int = CANVAS_WIDTH,
    font_size: int = DEFAULT_FONT_SIZE,
) -> bytes:
    """Render ``text`` to a fixed-width 2-column PNG and return raw bytes.

    The text is hard-wrapped to the per-column character width, split
    in half by line count, and drawn left-then-right with a fixed
    gutter. Pure-black ink on pure-white background — high contrast
    matters more for OCR accuracy than aesthetics here.
    """
    if not text:
        text = "(empty)"
    font = _find_mono_font(font_size)
    cw = _char_width(font) or 7.0

    col_width_px = (width - 2 * MARGIN - COLUMN_GAP) // 2
    chars_per_col = max(20, int(col_width_px // cw))

    lines = _wrap_text_hard(text, chars_per_col)
    half = (len(lines) + 1) // 2
    col1, col2 = lines[:half], lines[half:]
    rows = max(len(col1), len(col2))

    height = min(MAX_CANVAS_HEIGHT, MARGIN * 2 + rows * LINE_HEIGHT)
    img = Image.new("RGB", (width, height), color="white")
    draw = ImageDraw.Draw(img)

    # Column 1 — left
    y = MARGIN
    for line in col1:
        if y + LINE_HEIGHT > height - MARGIN:
            draw.text((MARGIN, y), "… [truncated]", font=font, fill="black")
            break
        draw.text((MARGIN, y), line, font=font, fill="black")
        y += LINE_HEIGHT

    # Column 2 — right
    col2_x = MARGIN + col_width_px + COLUMN_GAP
    y = MARGIN
    for line in col2:
        if y + LINE_HEIGHT > height - MARGIN:
            draw.text((col2_x, y), "… [truncated]", font=font, fill="black")
            break
        draw.text((col2_x, y), line, font=font, fill="black")
        y += LINE_HEIGHT

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
