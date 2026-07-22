"""ASA Copilot 意图结构化解析器（PRD 阶段 4 R9）。

纯规则引擎，不引入任何 LLM 自由判决。分三层：

1. 直写层（tier=direct）：沿用既有锚定正则的明确短句，逐字保留原语义，
   命中后仍走既有的直写链路（preflight → commit → 审计 → 幂等）。
2. 问句排除层：询问句/商量句一律不产生写入意图（零误写红线）。
3. 扩展动作词典（tier=confirm）：动作词 + 搭配 + 否定守卫识别出的
   复杂表达，不直写，产出 pending_intent 由用户确认后执行。

目标指代消歧（第三层的另一半）在 CoreService.copilot 内完成：
只有上下文能唯一定位到人岗关系（candidate context）时才产出 pending_intent。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any


# ---------------------------------------------------------------------------
# 第一层：直写层（legacy 锚定正则，逐字保留，语义不得改变）
# ---------------------------------------------------------------------------

_DIRECT_ACTION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "stop",
        (
            r"^(?:这个|该|当前)?(?:人选|候选人)?(?:复核|初筛|筛选)(?:不通过|未通过|失败)$",
            r"^(?:这个|该|当前)?(?:人选|候选人)?(?:停止推进|停止|淘汰)$",
            r"^(?:停止推进|停止|淘汰)(?:这个|该|当前)?(?:人选|候选人)?$",
        ),
    ),
    (
        "recommend",
        (
            r"^(?:这个|该|当前)?(?:人选|候选人)?(?:已经|已)?推荐(?:给客户)?(?:了)?$",
            r"^(?:已经|已)?(?:把)?(?:这个|该|当前)?(?:人选|候选人)推荐给客户(?:了)?$",
        ),
    ),
    (
        "contact",
        (
            r"^(?:这个|该|当前)?(?:人选|候选人)?(?:已经|已)?联系(?:过|了)?$",
            r"^(?:我)?(?:已经|已)?联系(?:过)?(?:这个|该|当前)?(?:人选|候选人)(?:了)?$",
        ),
    ),
    (
        "advance",
        (
            r"^(?:这个|该|当前)?(?:人选|候选人)?(?:复核|初筛|筛选)(?:通过|合格)$",
            r"^(?:复核|初筛|筛选)(?:通过|合格)(?:这个|该|当前)?(?:人选|候选人)?$",
            r"^(?:这个|该|当前)?(?:人选|候选人)?(?:可以|继续)推进$",
            r"^(?:继续推进)(?:这个|该|当前)?(?:人选|候选人)?$",
        ),
    ),
)


def _normalize(message: str) -> str:
    return re.sub(r"[\s，。！？!?、；;：:]+", "", str(message or ""))


def direct_candidate_action(message: str) -> str:
    """原 _explicit_candidate_action，逐字保留。"""
    text = _normalize(message)
    for action, patterns in _DIRECT_ACTION_RULES:
        if any(re.fullmatch(pattern, text) for pattern in patterns):
            return action
    return ""


def direct_candidate_update(message: str) -> str:
    """原 _explicit_candidate_update，逐字保留。"""
    raw = str(message or "")
    text = re.sub(r"\s+", "", raw)
    if not re.search(r"已读(?:不回|未回|没回|未回复|没有回复)", text):
        return ""
    question = re.search(r"(?:怎么|如何|怎么办|是否|要不要|能不能|可以吗)", text)
    write_intent = re.search(r"(?:记录(?:一下|下)?|备注(?:一下|下)?|标记(?:一下|下)?|更新(?:为|一下)?|同步|记一下)", text)
    if question and not re.search(r"(?:请|帮我|直接|记录下|备注下|标记为|更新为)", text):
        return ""
    if write_intent or re.fullmatch(r"(?:这个|该|当前)?(?:人选|候选人)?已读(?:不回|未回|没回|未回复|没有回复)", text):
        return "read_no_reply"
    return ""


# ---------------------------------------------------------------------------
# 第二层：问句排除（询问句/商量句零误写）
# ---------------------------------------------------------------------------

_QUESTION_TOKENS: tuple[str, ...] = (
    "怎么", "如何", "怎样", "是否", "要不要", "能不能", "可不可以",
    "行不行", "好不好", "为什么", "为何", "什么", "多少", "多久",
    "何时", "哪些", "哪里", "吗", "呢",
)


def is_question(message: str) -> bool:
    raw = str(message or "")
    if "?" in raw or "？" in raw:
        return True
    text = re.sub(r"\s+", "", raw)
    return any(token in text for token in _QUESTION_TOKENS)


# ---------------------------------------------------------------------------
# 第三层：扩展动作词典（确认层，命中后不直写，走 pending_intent 确认通道）
# ---------------------------------------------------------------------------

_NEGATION_TOKENS = ("没", "未", "不", "别", "莫")


def _negated(text: str, start: int) -> bool:
    """动作词前紧邻否定词则视为否定表达，不产生正向意图。"""
    prefix = text[max(0, start - 2):start]
    return any(token in prefix for token in _NEGATION_TOKENS)


# 顺序即优先级：stop 最先（“先别推给客户”必须落在 stop 而不是 recommend）。
_EXTENDED_ACTION_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "stop",
        "否定/放弃类表达，判定为停止推进",
        (
            r"不合适.{0,6}(?:停|别推|不推|淘汰|pass|PASS|Pass)",
            r"(?:不太行|不咋样|一般般|差点意思|不太合适).{0,6}(?:别推|不推|停|淘汰|pass|PASS|Pass)",
            r"先别(?:联系|推|推进|约|安排)",
            r"(?:客户|甲方|对方|用人方)说?(?:算了|不考虑了|不要了|不用了|先缓缓|再看看)",
            r"(?:放一放|放一边|先放着|搁置).{0,6}(?:不推进|不推|别推|停)",
            r"^(?:这个人|这人|他|她|该人选|这个候选人|当前人选)?(?:别推了|不推了|停了吧|停掉|淘汰吧|不推进了)$",
        ),
    ),
    (
        "recommend",
        "推给客户类表达，判定为已推荐给客户",
        (
            r"(?:推给客户|推给客户那边|发给客户|报给客户|推给甲方|发给甲方|发给用人方)",
            r"客户那边可以推",
        ),
    ),
    (
        "contact",
        "已沟通类表达，判定为已联系",
        (
            r"(?:聊过|沟通过|沟通过了|打过电话|通完电话|通了电话|加了微信|加上微信|对接上|联系上了)",
        ),
    ),
    (
        "advance",
        "肯定/推进类表达，判定为复核通过继续推进",
        (
            r"(?:可以|不错|挺好|挺好的|没问题|我看行|蛮好).{0,8}(?:约面试|安排面试|继续推|往下推|推进吧|往下走|下一轮)",
            r"(?:约面试|安排面试|约面|排面试)",
            r"(?:通过吧|给通过|算通过|直接通过)",
        ),
    ),
)


def extended_candidate_action(message: str) -> tuple[str, str]:
    """返回 (action, reason)；未命中返回 ("", "")。"""
    text = _normalize(message)
    if not text:
        return "", ""
    for action, reason, patterns in _EXTENDED_ACTION_RULES:
        for pattern in patterns:
            match = re.search(pattern, text)
            if match and not _negated(text, match.start()):
                return action, reason
    return "", ""


# ---------------------------------------------------------------------------
# 统一入口：结构化意图
# ---------------------------------------------------------------------------

def parse_candidate_intent(message: str) -> dict[str, Any]:
    """分层解析候选人写入意图。

    返回 {kind, action, target_scope, confidence, tier, reason}：
    - tier=direct：明确短句，沿用既有直写链路；
    - tier=confirm：扩展表达，只能走确认通道；
    - kind=none：无写入意图（询问句、否定句、无动作搭配）。
    """
    none_intent: dict[str, Any] = {
        "kind": "none", "action": "", "target_scope": "",
        "confidence": 0.0, "tier": "", "reason": "无写入意图",
    }
    action = direct_candidate_action(message)
    if action:
        return {
            "kind": "candidate_action", "action": action,
            "target_scope": "current_candidate", "confidence": 1.0,
            "tier": "direct", "reason": "明确指令短句（锚定正则）",
        }
    update = direct_candidate_update(message)
    if update:
        return {
            "kind": "candidate_update", "action": update,
            "target_scope": "current_candidate", "confidence": 1.0,
            "tier": "direct", "reason": "明确跟进记录短句（锚定正则）",
        }
    if is_question(message):
        return {**none_intent, "reason": "询问句/商量句，不产生写入意图"}
    action, reason = extended_candidate_action(message)
    if action:
        return {
            "kind": "candidate_action", "action": action,
            "target_scope": "current_candidate", "confidence": 0.85,
            "tier": "confirm", "reason": reason,
        }
    return none_intent


def intent_signature(kind: str, action: str, candidate_id: int, message: str) -> str:
    """pending_intent 防篡改签名：确认端点按同一算法重算比对。"""
    seed = f"copilot_intent|{kind}|{action}|{int(candidate_id)}|{message}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()
