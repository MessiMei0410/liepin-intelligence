"""候选人性别确定性推断（gender inference leaf module）。

客户硬性口径（长越科技/长川科技"不推进女性人选"）需要把性别做成结构化筛选
维度。本模块只做**铁证推断**，防误杀是第一条：

推断优先级（高 → 低）：
1. 简历结构化性别证据：「性别：男/女」字段、简历头部「男 | 28岁」「（女）」
   结构（source_profiles.full_text / candidate_profiles.profile_summary）；
2. display_name 称呼：「xx先生」→ male、「xx女士」→ female（遮罩名场景）；
3. 以上皆无 → unknown。

红线：unknown 一律不得排除，由调用方保留并标注「性别待核验」；只有推断为
female 的铁证才允许进入排除逻辑。

证据形态：返回 evidence = {"source", "field", "snippet"}，snippet 为命中原文
片段，供排除 reason 与人工审计引用。
"""

from __future__ import annotations

import re
from typing import Any

# 简历结构化性别字段：「性别：女」「性别: 男」「性别 女」
_GENDER_FIELD_RE = re.compile(r"性别\s*[:：]?\s*(男|女)")
# 简历头部结构：「男 | 28岁」「女|32 岁」「女 / 28岁」
_GENDER_HEADER_AGE_RE = re.compile(r"(?<![\u4e00-\u9fff])(男|女)\s*[|｜/]\s*\d{1,2}\s*岁")
# 姓名后括号性别：「张三（男）」「张三(女)」
_GENDER_PAREN_RE = re.compile(r"[(（]\s*(男|女)\s*[)）]")

_GENDER_LABEL = {"男": "male", "女": "female"}

# 口径便签 → 结构化开关的桥：便签文本命中以下任一性别排除词，
# filter-note commit 时把岗位 gender_requirement 置为 male_only（同一确认链）。
GENDER_MALE_ONLY_NOTE_TOKENS = (
    "不看女", "不要女", "不接收女", "不招女", "不推进女", "排除女",
    "限男", "仅男", "只要男", "只推男", "男性优先", "男士优先",
)


def _snippet(text: str, start: int, end: int, *, window: int = 12) -> str:
    lo = max(0, start - window)
    hi = min(len(text), end + window)
    return text[lo:hi].strip()


def _infer_from_text(text: str, *, field: str) -> dict[str, Any] | None:
    """从简历类文本提取结构化性别铁证；无命中返回 None。"""
    if not text:
        return None
    m = _GENDER_FIELD_RE.search(text)
    if m:
        return {
            "gender": _GENDER_LABEL[m.group(1)],
            "evidence": {
                "source": "resume_gender_field",
                "field": field,
                "snippet": _snippet(text, m.start(), m.end()),
            },
        }
    m = _GENDER_HEADER_AGE_RE.search(text[:500])  # 头部结构只看前 500 字
    if m:
        return {
            "gender": _GENDER_LABEL[m.group(1)],
            "evidence": {
                "source": "resume_header",
                "field": field,
                "snippet": _snippet(text, m.start(), m.end()),
            },
        }
    m = _GENDER_PAREN_RE.search(text[:200])
    if m:
        return {
            "gender": _GENDER_LABEL[m.group(1)],
            "evidence": {
                "source": "resume_header",
                "field": field,
                "snippet": _snippet(text, m.start(), m.end()),
            },
        }
    return None


def infer_gender(
    display_name: str = "",
    profile_summary: str = "",
    resume_text: str = "",
) -> dict[str, Any]:
    """确定性性别推断：返回 {"gender": male/female/unknown, "evidence": {...}|None}。

    优先级：简历结构化证据（profile_summary → resume_text）> display_name 称呼。
    任何一步命中即返回；全部无命中 → unknown（调用方必须保留，不得排除）。
    """
    for text, field in (
        (str(profile_summary or ""), "candidate_profiles.profile_summary"),
        (str(resume_text or ""), "source_profiles.full_text"),
    ):
        hit = _infer_from_text(text, field=field)
        if hit:
            return hit
    name = str(display_name or "").strip()
    if name.endswith("先生"):
        return {
            "gender": "male",
            "evidence": {"source": "display_name_honorific", "field": "people.display_name", "snippet": name},
        }
    if name.endswith("女士"):
        return {
            "gender": "female",
            "evidence": {"source": "display_name_honorific", "field": "people.display_name", "snippet": name},
        }
    return {"gender": "unknown", "evidence": None}


def detect_male_only_note(note_text: str) -> str:
    """口径便签 → 结构化开关的桥：便签含性别排除词时返回 'male_only'，否则 ''。"""
    text = str(note_text or "")
    return "male_only" if any(token in text for token in GENDER_MALE_ONLY_NOTE_TOKENS) else ""
