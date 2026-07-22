#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def font(size: int, weight: str = "Bold") -> ImageFont.FreeTypeFont:
    candidates = [
        f"/System/Library/Fonts/SFNS.ttf",
        f"/System/Library/Fonts/Supplemental/Arial {weight}.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def rounded_gradient(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gradient = Image.new("RGBA", (size, size), (0, 0, 0, 255))
    pixels = gradient.load()
    for y in range(size):
        for x in range(size):
            t = (x * 0.46 + y * 0.54) / max(1, size - 1)
            r = int(17 * (1 - t) + 10 * t)
            g = int(24 * (1 - t) + 16 * t)
            b = int(39 * (1 - t) + 32 * t)
            pixels[x, y] = (r, g, b, 255)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [int(size * 0.0625), int(size * 0.0625), int(size * 0.9375), int(size * 0.9375)],
        radius=int(size * 0.21),
        fill=255,
    )
    image.alpha_composite(gradient)
    image.putalpha(mask)
    return image


def draw_icon(size: int) -> Image.Image:
    scale = size / 1024
    image = rounded_gradient(size)
    draw = ImageDraw.Draw(image)

    def pt(x: float, y: float) -> tuple[int, int]:
        return (round(x * scale), round(y * scale))

    muted = (51, 65, 85, 164)
    draw.line([pt(250, 354), pt(360, 294), pt(464, 276), pt(588, 316), pt(733, 439)], fill=muted, width=round(28 * scale), joint="curve")
    active = (79, 140, 255, 255)
    draw.line([pt(214, 668), pt(330, 534), pt(478, 462), pt(637, 553), pt(741, 625), pt(829, 573)], fill=active, width=round(38 * scale), joint="curve")
    for x, y, r, color in [
        (260, 668, 38, (45, 212, 191, 255)),
        (512, 462, 42, (96, 165, 250, 255)),
        (758, 624, 38, (167, 243, 208, 255)),
    ]:
        draw.ellipse([pt(x - r, y - r), pt(x + r, y + r)], fill=color)

    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle(
        [*pt(226, 308), *pt(798, 764)],
        radius=round(106 * scale),
        fill=(2, 6, 23, 70),
    )
    image.alpha_composite(shadow)

    card = [*pt(226, 284), *pt(798, 740)]
    draw.rounded_rectangle(card, radius=round(106 * scale), fill=(248, 250, 252, 255))
    label_font = font(round(188 * scale))
    text = "ASA"
    bbox = draw.textbbox((0, 0), text, font=label_font)
    x = round(size / 2 - (bbox[2] - bbox[0]) / 2)
    y = round(585 * scale - (bbox[3] - bbox[1]) * 0.74)
    draw.text((x, y), text, font=label_font, fill=(17, 24, 39, 255))
    draw.line([pt(326, 638), pt(698, 638)], fill=(37, 99, 235, 255), width=round(18 * scale))
    return image


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--size", type=int, default=1024)
    args = parser.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    draw_icon(args.size).save(out)


if __name__ == "__main__":
    main()
