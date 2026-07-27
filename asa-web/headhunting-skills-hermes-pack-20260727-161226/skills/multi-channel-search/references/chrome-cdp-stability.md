# CDP 渠道稳定性记录

## X-SaaS 搜索（稳定 ✅）

通过 `xhs_cdp.py` 的 CDP 原始 WebSocket 类连接 Chrome 9223 端口，稳定提取候选人数据。

```python
from xhs_cdp import CDP
cdp = CDP(ws_url)
result = cdp.send("Runtime.evaluate", {
    "expression": "document.querySelectorAll('tbody tr')...",
    "returnByValue": True
})
```

关键点：
- X-SaaS 是 Vue SPA，用 `location.hash` 导航后等 5 秒加载
- 提取 `tbody tr > td` 文本数组（15+列）
- 过滤匿名行（姓名字段含"先生/女士"或纯公司名）

## 猎聘搜索（正常 ✅ — 但 VIP 受限 ⚠️）

**猎聘常规搜索**（`www.liepin.com/zhaopin/`）正常可用，含人才卡片。

**猎聘 VIP 后台**（`vip.liepin.com`）返回：
```
403 Forbidden
Powered by Tengine
```

即使用 Chrome 9223 CDP 也无法绕过。如需 VIP 功能请走手机端或联系猎聘 BD。

## 当前工作流

1. X-SaaS → CDP WebSocket → 提取候选人 → 入库（来源标记 X-SaaS）
2. 猎聘常规搜索 → CDP 逐变体搜索 → 提取卡片 → 入库（来源标记 liepin）
3. 合并去重（两渠道 0 重叠，互补性极强）

## 数据提取样例

X-SaaS 返回格式（每行 15+ 列）：
```
陈文雄 5153100  1 精 大工业库 | 深圳鹏新旭技术有限公司 技术经理 | 男 | 47岁 | ...
```
解析：姓名 + 候选ID + 简历数 + 人才库标签 + 公司 + 职位 + 性别 + 年龄...
