"""岗位状态过滤：可入库/可推荐名单与不可入库黑名单关键词（2026-07-22 产品裁决）。

三处共用同一语义，改动时必须同步：
- 本文件（a_system_agent/service.py 等 Python 侧使用）
- /Users/messi/Documents/Codex/2026-06-26/re/work/talent_system_sync.py（内置同名单副本）
- liepin-intelligence/liepin-reply-assistant-extension/content.js（JS 内置同名单副本）

jobs.status 为自由文本、无枚举。黑名单关键词优先于一切；未列出的新状态默认
按可入库处理（保守不阻断业务）。
"""

from __future__ import annotations

# 可入库/可推荐（白名单语义，作文档与测试参考；未列出状态默认可入库）
JOB_INTAKE_ALLOWED_STATUSES = (
    "已发布",
    "已发布/推进中",
    "已搜索/可筛人",
    "谈薪中",
    "有反馈/待复盘",
    "有搜索计划",
)

# 不可入库黑名单：status 命中任一关键词（子串、忽略大小写）即不可入库/不可推荐
JOB_INTAKE_BLOCKED_KEYWORDS = (
    "待启动",
    "暂停",
    "关闭",
    "closed",
    "只读快照",
    "已拆分",
    "误归属",
    "归档",
)


def job_status_intake_blocked(status: object) -> bool:
    text = "" if status is None else str(status).strip().lower()
    if not text:
        return False
    return any(keyword.lower() in text for keyword in JOB_INTAKE_BLOCKED_KEYWORDS)


def job_status_intake_allowed(status: object) -> bool:
    return not job_status_intake_blocked(status)
