"""Generate the ACM extension Marketplace icon (128x128 PNG).

Reproduces the activity-bar "stacked context layers" motif (see icon.svg)
on a branded indigo->blue gradient. Rendered at 4x and downscaled with
LANCZOS for clean anti-aliased edges. Pure Pillow, no SVG toolchain needed.
"""
from __future__ import annotations

from PIL import Image, ImageDraw

S = 512          # supersampled canvas
OUT = 128        # final size
R = 96           # corner radius @ 4x


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def main() -> None:
    top = (109, 94, 246)     # indigo  #6D5EF6
    bot = (59, 130, 246)     # blue    #3B82F6

    # vertical gradient
    grad = Image.new("RGB", (S, S))
    px = grad.load()
    for y in range(S):
        c = lerp(top, bot, y / (S - 1))
        for x in range(S):
            px[x, y] = c

    # rounded-rect mask
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=R, fill=255)

    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img, "RGBA")

    # isometric "layers" stack — centred
    cx = S // 2
    hw = 150          # half width of a diamond
    hh = 86           # half height of a diamond
    gap = 92          # vertical gap between layers
    top_y = 168       # centre y of the top diamond
    lw = 18           # stroke width

    def diamond(cy, fill, outline):
        pts = [(cx, cy - hh), (cx + hw, cy), (cx, cy + hh), (cx - hw, cy)]
        d.polygon(pts, fill=fill, outline=outline, width=lw)

    white = (255, 255, 255, 255)
    # back two layers as outlined connecting edges (V shapes)
    for i in (2, 1):
        cy = top_y + i * gap
        d.line([(cx - hw, cy - hh), (cx, cy), (cx + hw, cy - hh)],
               fill=(255, 255, 255, 235), width=lw, joint="curve")
    # front (top) solid diamond
    diamond(top_y, (255, 255, 255, 250), white)

    img = img.resize((OUT, OUT), Image.LANCZOS)
    img.save("media/icon.png")
    print("wrote media/icon.png", img.size)


if __name__ == "__main__":
    main()
