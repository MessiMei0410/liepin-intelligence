"""小红书封面图生成器 — Pillow 纯文字科技风竖版封面

用法：
    python3 generate_cover.py "标题" "副标题" "标签" output.png

色系选项：blue(科技蓝) / purple(数据紫) / teal(青绿)
"""

import sys
import os
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1440
FONT_PATH = "/System/Library/Fonts/Hiragino Sans GB.ttc"

PALETTES = {
    "blue":   {"bg_top": (5,10,40), "bg_bot": (15,40,90), "accent": (80,160,255)},
    "purple": {"bg_top": (25,5,40), "bg_bot": (60,15,90), "accent": (160,100,255)},
    "teal":   {"bg_top": (5,30,30), "bg_bot": (10,65,65), "accent": (60,210,190)},
}


def draw_shadow(draw, xy, text, font, fill):
    x, y = xy
    draw.text((x + 3, y + 3), text, fill=(0, 0, 0, 100), font=font)
    draw.text((x, y), text, fill=fill, font=font)


def generate(title, subtitle, tag, palette_name, out_path):
    p = PALETTES.get(palette_name, PALETTES["blue"])
    accent = p["accent"]

    title_font = ImageFont.truetype(FONT_PATH, 80)
    sub_font = ImageFont.truetype(FONT_PATH, 40)
    tag_font = ImageFont.truetype(FONT_PATH, 30)

    img = Image.new("RGBA", (W, H), (*p["bg_top"], 255))
    draw = ImageDraw.Draw(img)

    # 渐变
    for y in range(H):
        r = int(p["bg_top"][0] + (p["bg_bot"][0] - p["bg_top"][0]) * y / H)
        g = int(p["bg_top"][1] + (p["bg_bot"][1] - p["bg_top"][1]) * y / H)
        b = int(p["bg_top"][2] + (p["bg_bot"][2] - p["bg_top"][2]) * y / H)
        draw.line([(0, y), (W, y)], fill=(r, g, b, 255))

    # 装饰圆
    for cx, cy, r, a in [(750, 350, 200, 15), (250, 1000, 240, 10), (950, 1200, 160, 12)]:
        o = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(o).ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*accent, a))
        img = Image.alpha_composite(img, o)
    draw = ImageDraw.Draw(img)

    # 标签胶囊
    tw = draw.textlength(tag, font=tag_font)
    draw.rounded_rectangle([80, 80, 120 + tw, 140], radius=25, fill=(*accent, 255))
    draw.text((100, 86), tag, fill="white", font=tag_font)

    # 标题
    draw_shadow(draw, (80, H // 2 - 150), title, title_font, "white")

    # 分隔线
    draw.rounded_rectangle([80, H // 2 - 40, 200, H // 2 - 32], radius=3, fill=(*accent, 255))

    # 副标题
    draw_shadow(draw, (80, H // 2 + 10), subtitle, sub_font, (220, 220, 255, 255))

    # 底部指示点
    for i in range(4):
        draw.ellipse([80 + i * 55, H - 100, 92 + i * 55, H - 88], fill=(*accent, 200))

    img.convert("RGB").save(out_path, quality=95)
    print(f"✓ {out_path}")


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("用法: python3 generate_cover.py <标题> <副标题> <标签> <输出路径> [色系:blue|purple|teal]")
        sys.exit(1)

    title = sys.argv[1]
    subtitle = sys.argv[2]
    tag = sys.argv[3]
    out = sys.argv[4]
    palette = sys.argv[5] if len(sys.argv) > 5 else "blue"

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    generate(title, subtitle, tag, palette, out)
