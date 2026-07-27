# 候选人评分参考

基于猎聘搜索结果卡片摘要文本（`.tlog-common-resume-card` textContent）自动打分。

## 评分维度

| 维度 | 条件 | 分值 |
|------|------|------|
| 学历+专业 | 光学/光电/光子/激光 博士 | +30 |
| | 其他方向 博士 | +10 |
| | 光学工程/光电信息/光信息/光学/激光/精密仪器/光电子 硕士 | +20 |
| | 物理/材料/电子科学/仪器/测控 硕士 | +10 |
| | 非光学专业 硕士 | +0 |
| | 光电/光学/光信息 本科 | +10 |
| 目标公司 | ASML/KLA/SMEE/蔡司/舜宇/茂莱/御微/天准/海思光/Lumileds/禾赛 | +15 |
| | 华为/歌尔光学/海康/Newport/Thorlabs/大族激光 | +8~12 |
| PM经验 | 产品经理/产品总监/产品线/产品负责人/产品部长/项目经理/研发经理 | +10 |
| 光学工具 | Zemax/CodeV/LightTools/Tracepro | +10 |
| 半导体 | 半导体/光刻/晶圆/Stage/量测/精密光学/对准 | +5 |

## 评级阈值

| 等级 | 分数 | 含义 |
|------|------|------|
| 🔥🔥🔥 | ≥40 | 重点推荐：专业+公司+PM+工具 四维匹配 |
| 🔥🔥 | 25-39 | 值得关注：2-3维匹配 |
| 🔥 | 15-24 | 光学背景但其他维度弱 |
| ⚠️ | 5-14 | 勉强相关 |
| ❌ | <5 | 排除（非光学专业/学历不达标/完全不匹配） |

## 排除规则

以下情况直接 pass，不打分：
- 非光学相关专业（机械/电子/自动化/材料化学/工商管理等）
- 学历不达标（中专/大专/高中）
- 纯消费电子光学（手机镜头）且无精密/工业/半导体经验

## 使用示例

```python
t = card_text  # 从 .tlog-common-resume-card textContent 获取
score = 0

# 学历
if '博士' in t and any(k in t[:400] for k in ['光学','光电','光子','激光']):
    score += 30
elif '硕士' in t and any(k in t[:400] for k in ['光学工程','光电信息','光信息','光学','激光']):
    score += 20

# 公司
for comp in ['ASML','KLA','SMEE','蔡司','舜宇','茂莱','御微','海思光','Lumileds']:
    if comp in t: score += 15; break

# PM
if any(ti in t for ti in ['产品经理','产品总监','产品线','产品负责人']):
    score += 10

# 工具
if any(tool in t for tool in ['Zemax','CodeV','LightTools']):
    score += 10

# 半导体
if any(k in t for k in ['半导体','光刻','晶圆']):
    score += 5

grade = '🔥🔥🔥' if score>=40 else '🔥🔥' if score>=25 else '🔥' if score>=15 else '⚠️'
```
