"""Generate the ACM logo lockup (horizontal wordmark + mark).

Reuses the extension icon's "stacked context layers" mark on a branded
indigo->blue gradient, paired with an "ACM" wordmark and tagline.
Renders at 3x and downscales (LANCZOS) onto a transparent canvas.
Outputs:
  - media/logo.png        full lockup (mark + wordmark + tagline)
  - media/logo-dark.png   same, light text for dark backgrounds
Pure Pillow; uses system fonts (Arial). No SVG toolchain needed.
"""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

SCALE = 3
INDIGO = (109, 94, 246)
BLUE = (59, 130, 246)
INK = (30, 41, 59)        # slate-800
INK_SUB = (100, 116, 139)  # slate-500
LIGHT = (241, 245, 249)   # slate-100
LIGHT_SUB = (148, 163, 184)  # slate-400

FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_REG = "/System/Library/Fonts/Supplemental/Arial.ttf"


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def make_mark(size: int) -> Image.Image:
    """Rounded-square gradient tile with the isometric layers stack."""
    grad = Image.new("RGB", (size, size))
    px = grad.load()
    for y in range(size):
        c = lerp(INDIGO, BLUE, y / (size - 1))
        for x in range(size):
            px[x, y] = c
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * 0.23), fill=255)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img, "RGBA")
    cx = size // 2
    hw = int(size * 0.30)
    hh = int(size * 0.17)
    gap = int(size * 0.18)
    top_y = int(size * 0.33)
    lw = max(2, int(size * 0.035))
    for i in (2, 1):
        cy = top_y + i * gap
        d.line([(cx - hw, cy - hh), (cx, cy), (cx + hw, cy - hh)],
               fill=(255, 255, 255, 235), width=lw, joint="curve")
    pts = [(cx, top_y - hh), (cx + hw, top_y), (cx, top_y + hh), (cx - hw, top_y)]
    d.polygon(pts, fill=(255, 255, 255, 250), outline=(255, 255, 255, 255), width=lw)
    return img


def build(out: str, ink, ink_sub) -> None:
    s = SCALE
    H = 110 * s
    mark_sz = 96 * s
    pad = 8 * s

    word = ImageFont.truetype(FONT_BOLD, 64 * s)
    tag = ImageFont.truetype(FONT_REG, 23 * s)
    word_txt, tag_txt = "ACM", "Context Management"

    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    wb = tmp.textbbox((0, 0), word_txt, font=word)
    tb = tmp.textbbox((0, 0), tag_txt, font=tag, anchor="la")
    word_w = wb[2] - wb[0]
    tag_w = tb[2] - tb[0]
    text_w = max(word_w, tag_w)

    gap = 28 * s
    W = pad + mark_sz + gap + text_w + pad
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    mark = make_mark(mark_sz)
    img.alpha_composite(mark, (pad, (H - mark_sz) // 2))

    d = ImageDraw.Draw(img)
    tx = pad + mark_sz + gap
    # vertically centre the two-line text block
    block_h = (wb[3] - wb[1]) + 12 * s + (tb[3] - tb[1])
    y0 = (H - block_h) // 2
    d.text((tx - wb[0], y0 - wb[1]), word_txt, font=word, fill=ink)
    ty = y0 + (wb[3] - wb[1]) + 12 * s
    d.text((tx - tb[0], ty - tb[1]), tag_txt, font=tag, fill=ink_sub)

    img = img.resize((W // s, H // s), Image.LANCZOS)
    img.save(out)
    print("wrote", out, img.size)


if __name__ == "__main__":
    build("media/logo.png", INK, INK_SUB)
    build("media/logo-dark.png", LIGHT, LIGHT_SUB)
