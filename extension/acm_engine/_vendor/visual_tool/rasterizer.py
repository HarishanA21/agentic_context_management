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
MAX_CANVAS_HEIGHT = 8192  # safety cap (single-image back-compat path)

# Per-page height for the paginated renderer. Kept small so the long edge
# stays under the threshold where vision models downscale (~1568 px for
# Claude, ~768 short-edge for GPT-4o). A tall single strip gets squashed
# ~5x and the text becomes unreadable; a stack of short pages does not.
PAGE_HEIGHT = 1280
MAX_PAGES = 8            # bound cost — at ~156 lines/page that's ~1.2k lines


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


def _draw_column(draw, lines, x, font, page_height):
    """Draw one column of pre-wrapped lines starting at ``x``."""
    y = MARGIN
    for line in lines:
        if y + LINE_HEIGHT > page_height - MARGIN:
            break
        draw.text((x, y), line, font=font, fill="black")
        y += LINE_HEIGHT


def _render_one_page(
    col1: list[str],
    col2: list[str],
    *,
    width: int,
    font,
    col_width_px: int,
    page_height: int,
    header: Optional[str] = None,
) -> bytes:
    """Render a single 2-column page to PNG bytes."""
    rows = max(len(col1), len(col2))
    height = min(page_height, MARGIN * 2 + rows * LINE_HEIGHT)
    if header:
        height = min(page_height, height + LINE_HEIGHT)
    img = Image.new("RGB", (width, height), color="white")
    draw = ImageDraw.Draw(img)
    _draw_column(draw, col1, MARGIN, font, height)
    _draw_column(draw, col2, MARGIN + col_width_px + COLUMN_GAP, font, height)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_2col_pages(
    text: str,
    *,
    width: int = CANVAS_WIDTH,
    font_size: int = DEFAULT_FONT_SIZE,
    page_height: int = PAGE_HEIGHT,
    max_pages: int = MAX_PAGES,
) -> list[bytes]:
    """Render ``text`` to a *list* of fixed-width 2-column PNG pages.

    The text is hard-wrapped to the per-column character width, then laid
    out column-by-column, page-by-page. Each page is at most
    ``page_height`` px tall so the long edge stays under the size where
    vision models downscale — keeping the rendered text legible (a single
    tall strip gets squashed ~5x and becomes unreadable). Returns one PNG
    per page; capped at ``max_pages`` with a truncation note on the last
    page when content overflows.
    """
    if not text:
        text = "(empty)"
    font = _find_mono_font(font_size)
    cw = _char_width(font) or 7.0

    col_width_px = (width - 2 * MARGIN - COLUMN_GAP) // 2
    chars_per_col = max(20, int(col_width_px // cw))

    lines = _wrap_text_hard(text, chars_per_col)
    lines_per_col = max(1, (page_height - 2 * MARGIN) // LINE_HEIGHT)
    lines_per_page = lines_per_col * 2

    # Split into page-sized chunks of lines, then each chunk into 2 columns.
    pages: list[bytes] = []
    total = len(lines)
    n_pages = (total + lines_per_page - 1) // lines_per_page
    capped = min(n_pages, max_pages)
    for p in range(capped):
        chunk = lines[p * lines_per_page : (p + 1) * lines_per_page]
        # On the final allowed page, if more lines remain, flag truncation.
        if p == capped - 1 and capped < n_pages:
            dropped = total - (capped * lines_per_page)
            chunk = chunk[: lines_per_page - 1]
            chunk.append(f"… [truncated {dropped} more line(s) — see REFERENCES]")
        col1 = chunk[:lines_per_col]
        col2 = chunk[lines_per_col:]
        pages.append(
            _render_one_page(
                col1, col2,
                width=width, font=font,
                col_width_px=col_width_px, page_height=page_height,
            )
        )
    return pages or [
        _render_one_page(
            ["(empty)"], [], width=width, font=font,
            col_width_px=col_width_px, page_height=page_height,
        )
    ]


def render_2col(
    text: str,
    *,
    width: int = CANVAS_WIDTH,
    font_size: int = DEFAULT_FONT_SIZE,
) -> bytes:
    """Back-compat single-image renderer — returns the first page only.

    New callers should use :func:`render_2col_pages` so large outputs stay
    legible across multiple pages instead of one downscaled strip.
    """
    return render_2col_pages(text, width=width, font_size=font_size)[0]
