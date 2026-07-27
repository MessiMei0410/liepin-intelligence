# Pillow 生成小红书配图

纯 Python/Pillow 生成竖版封面图，无需 GPU。适合快速批量出图。

## 规格

- 尺寸：1080 × 1440（3:4 竖版）
- 字体：Hiragino Sans GB（macOS 系统自带，中文清晰）
- 格式：PNG，quality=95
- 标题字号 80px，副标题 38px，标签 28-30px

## 核心代码模板

```python
from PIL import Image, ImageDraw, ImageFont
W, H = 1080, 1440

# 字体
title_font = ImageFont.truetype("/System/Library/Fonts/Hiragino Sans GB.ttc", 80)
sub_font = ImageFont.truetype("/System/Library/Fonts/Hiragino Sans GB.ttc", 38)
tag_font = ImageFont.truetype("/System/Library/Fonts/Hiragino Sans GB.ttc", 28)

# 渐变背景
img = Image.new('RGBA', (W, H), (*bg_top, 255))
draw = ImageDraw.Draw(img)
for y in range(H):
    r = int(bg_top[0] + (bg_bot[0]-bg_top[0]) * y/H)
    g = int(bg_top[1] + (bg_bot[1]-bg_top[1]) * y/H)
    b = int(bg_top[2] + (bg_bot[2]-bg_top[2]) * y/H)
    draw.line([(0,y), (W,y)], fill=(r,g,b,255))

# 文字阴影增强对比度
draw.text((x+3, y+3), text, fill=(0,0,0,80), font=font)  # 阴影
draw.text((x, y), text, fill='white', font=font)          # 主体
```

## 装饰元素

三种已验证的风格模板：

| 主题 | 配色 | 装饰 |
|------|------|------|
| 芯片/AI | 深蓝 (5,10,40)→(15,40,90) | 网格 + 电路走线 + 发光节点 + 芯片图标 |
| 数据/匹配 | 紫 (20,5,40)→(60,15,90) | 节点网络 + 连线 + 中心辐射 |
| 标签/组织 | 青绿 (5,25,30)→(10,65,65) | 浮动卡片标签 + AI引擎 + 连线 |

## 保存位置

- 新配图 → `~/Desktop/小红书/待发/配图/`
- 发布后 → `~/Desktop/小红书/已发/配图/`

## Pitfalls

1. **字体文件路径**：不同 macOS 版本字体路径可能不同，先 `ls /System/Library/Fonts/` 确认
2. **pypi 镜像**：清华源 (tuna.tsinghua) 可能 403，切换到 `https://mirrors.aliyun.com/pypi/simple/`
3. **色差**：Pillow 保存的 PNG 在深色背景下可能有轻微色偏，用 `quality=95` 缓解
4. **Rounded rectangle**：Pillow 10.0+ 支持 `draw.rounded_rectangle()`，旧版需手动绘制
