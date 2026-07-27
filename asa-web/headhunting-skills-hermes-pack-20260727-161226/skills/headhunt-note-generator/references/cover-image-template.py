"""小红书封面图生成模板 — 1080×1440 竖版，科技风。
使用前确保 Pillow 已安装。阿里云镜像：
  python3 -m pip install -i https://mirrors.aliyun.com/pypi/simple/ Pillow
"""

from PIL import Image, ImageDraw, ImageFont
import os, math, random

W, H = 1080, 1440
FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"

def draw_shadow(draw, xy, text, font, fill):
    x, y = xy
    draw.text((x+3, y+3), text, fill=(0,0,0,80), font=font)
    draw.text((x, y), text, fill=fill, font=font)

def glow_ellipse(draw, cx, cy, r, color, alpha):
    for i in range(3):
        a = alpha * (0.3 + 0.25*i)
        rr = r * (1 - 0.3*i)
        draw.ellipse([cx-rr, cy-rr, cx+rr, cy+rr], fill=(*color, int(a)))

# 配色方案:
# 芯片/半导体: bg_top=(5,10,40), bg_bot=(15,40,90), accent=(80,160,255)
# AI/招聘:     bg_top=(20,5,40), bg_bot=(60,15,90), accent=(160,100,255)
# 数据/标签:    bg_top=(5,30,30), bg_bot=(10,65,65), accent=(60,210,190)

def make_cover(title, subtitle, tag, bg_top, bg_bot, accent, decor_style, out_path):
    img = Image.new('RGBA', (W, H), (*bg_top, 255))
    draw = ImageDraw.Draw(img)
    tf = ImageFont.truetype(FONT, 80)
    sf = ImageFont.truetype(FONT, 38)
    tagf = ImageFont.truetype(FONT, 28)

    # 渐变背景
    for y in range(H):
        r = int(bg_top[0] + (bg_bot[0]-bg_top[0]) * y/H)
        g = int(bg_top[1] + (bg_bot[1]-bg_top[1]) * y/H)
        b = int(bg_top[2] + (bg_bot[2]-bg_top[2]) * y/H)
        draw.line([(0,y), (W,y)], fill=(r,g,b,255))

    # 装饰元素 — 按主题切换
    if decor_style == "circuit":   # 电路板风
        for x in range(0, W, 60):
            draw.line([(x, 0), (x, H)], fill=(*accent, 10), width=1)
        for y in range(0, H, 60):
            draw.line([(0, y), (W, y)], fill=(*accent, 10), width=1)
        for _ in range(8):
            y1 = random.randint(100, H-200)
            x1 = random.randint(50, W-50)
            x2 = x1 + random.choice([-200, 200])
            draw.line([(x1,y1),(x2,y1)], fill=(*accent, 40), width=2)
            draw.line([(x2,y1),(x2,y1+random.randint(50,150))], fill=(*accent, 40), width=2)
        for _ in range(15):
            cx, cy = random.randint(80,W-80), random.randint(150,H-300)
            glow_ellipse(draw, cx, cy, random.randint(4,12), accent, 80)

    elif decor_style == "network":  # 数据网络风
        nodes = [(random.randint(100,W-100), random.randint(200,H-400)) for _ in range(20)]
        for i in range(len(nodes)):
            for j in range(i+1, len(nodes)):
                if random.random() < 0.15 and abs(nodes[i][0]-nodes[j][0]) < 300:
                    draw.line([nodes[i], nodes[j]], fill=(*accent, 25), width=1)
        for nx, ny in nodes:
            r = random.randint(5, 15)
            glow_ellipse(draw, nx, ny, r*2, accent, 30)

    elif decor_style == "tags":     # 标签云风
        labels = ["GaN器件", "7nm", "模拟前端", "车规级", "高压驱动", "射频芯片", "数字后端"]
        for lb in labels:
            x, y = random.randint(80, W//2+100), random.randint(250, 900)
            cw = len(lb)*25 + 30
            draw.rounded_rectangle([x,y,x+cw,y+50], radius=15, fill=(*accent,30), outline=(*accent,80), width=2)
            draw.text((x+15, y+8), lb, fill=(*accent, 200), font=tagf)

    # 标签胶囊
    tw = draw.textlength(tag, font=tagf)
    draw.rounded_rectangle([80, 80, 120+tw, 140], radius=25, fill=(*accent, 255))
    draw.text((100, 86), tag, fill='white', font=tagf)

    # 标题 + 分隔线 + 副标题
    draw_shadow(draw, (80, H//2-150), title, tf, 'white')
    draw.rounded_rectangle([80, H//2-40, 200, H//2-32], radius=3, fill=(*accent, 255))
    draw_shadow(draw, (80, H//2+10), subtitle, sf, (220, 220, 255, 255))

    # 底部指示点
    for i in range(4):
        draw.ellipse([80+i*55, H-100, 92+i*55, H-88], fill=(*accent, 200))

    img.convert('RGB').save(out_path, quality=95)
