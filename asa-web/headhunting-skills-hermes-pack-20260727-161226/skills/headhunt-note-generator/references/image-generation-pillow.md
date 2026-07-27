# 小红书配图生成 — Pillow 代码模板

## 字体

macOS 中文字体路径: `/System/Library/Fonts/Hiragino Sans GB.ttc`

## 标准模板

```python
from PIL import Image, ImageDraw, ImageFont
import os, math, random

W, H = 1080, 1440
FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"
title_font = ImageFont.truetype(FONT, 80)
sub_font = ImageFont.truetype(FONT, 38)
tag_font = ImageFont.truetype(FONT, 28)

def draw_shadow(draw, xy, text, font, fill):
    x, y = xy
    draw.text((x+3, y+3), text, fill=(0,0,0,80), font=font)
    draw.text((x, y), text, fill=fill, font=font)

def make_card(title, subtitle, tag, bg_top, bg_bot, accent, filename):
    img = Image.new('RGBA', (W, H), (*bg_top, 255))
    draw = ImageDraw.Draw(img)
    
    # 渐变背景
    for y in range(H):
        r = int(bg_top[0] + (bg_bot[0]-bg_top[0]) * y/H)
        g = int(bg_top[1] + (bg_bot[1]-bg_top[1]) * y/H)
        b = int(bg_top[2] + (bg_bot[2]-bg_top[2]) * y/H)
        draw.line([(0,y), (W,y)], fill=(r,g,b,255))
    
    # 装饰圆
    dots = [(750, 350, 200, 15), (250, 1000, 240, 10), (950, 1200, 160, 12)]
    for cx, cy, r, a in dots:
        o = Image.new('RGBA', (W,H), (0,0,0,0))
        ImageDraw.Draw(o).ellipse([cx-r,cy-r,cx+r,cy+r], fill=(*accent, a))
        img = Image.alpha_composite(img, o)
    draw = ImageDraw.Draw(img)
    
    # 标签胶囊
    tw = draw.textlength(tag, font=tag_font)
    draw.rounded_rectangle([80, 80, 120+tw, 140], radius=25, fill=(*accent, 255))
    draw.text((100, 86), tag, fill='white', font=tag_font)
    
    # 标题
    draw_shadow(draw, (80, H//2-150), title, title_font, 'white')
    
    # 分隔线
    draw.rounded_rectangle([80, H//2-40, 200, H//2-32], radius=3, fill=(*accent, 255))
    
    # 副标题
    draw_shadow(draw, (80, H//2+10), subtitle, sub_font, (220, 220, 255, 255))
    
    # 底部点
    for i in range(4):
        draw.ellipse([80+i*55, H-100, 92+i*55, H-88], fill=(*accent, 200))
    
    img.convert('RGB').save(filename, quality=95)
```

## 配色方案

| 主题 | bg_top | bg_bot | accent |
|------|--------|--------|--------|
| 芯片/AI | (5,10,40) | (15,40,90) | (80,160,255) |
| 数据/招聘 | (25,5,40) | (60,15,90) | (160,100,255) |
| 人才/组织 | (5,30,30) | (10,65,65) | (60,210,190) |

## 装饰元素（可选增强）

```python
# 电路板风 — 芯片主题
for x in range(0, W, 60): draw.line([(x,0),(x,H)], fill=(40,60,120,15), width=1)
for _ in range(8): draw.line([(x1,y1),(x2,y1)], fill=(80,160,255,60), width=2)

# 数据网络风 — 招聘主题
nodes = [(random.randint(100,W-100), random.randint(200,H-400)) for _ in range(20)]
for i,j in combinations: draw.line([nodes[i], nodes[j]], fill=(160,100,255,30), width=1)

# 标签云风 — 人才主题
for label in ['GaN','7nm','模拟前端','车规级']:
    draw.rounded_rectangle([x, y, x+w, y+50], radius=15, fill=(60,210,190,40))
```
