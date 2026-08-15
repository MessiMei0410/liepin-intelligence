"""Copilot intent detection and message interpretation (split from copilot_handler.py).

All functions receive 'self' (AgentService instance) as first parameter where present.
"""

from __future__ import annotations
import re, sqlite3
from typing import Any

from ._shared import (
    _loads,
    _is_short_ack,
    _table_exists,
)
from . import company_kb
from . import talent_intel
from .llm import LLMError
from .privacy import sanitize_payload
from .conversation_state import (
    TERMINAL_WORKFLOW_STATUSES,
    enrich_turn_understanding,
)

# Cross-module references (split from copilot_handler.py)
from .copilot_evidence import _copilot_context_job_id, _is_job_budget_fact_update, _jobs_relevant_to_selected_context, _new_candidate_outreach_requested, _pending_sourcing_refinement_mode, _strategy_revision_requested


@staticmethod
def _copilot_action_kind(message: str) -> str:
    if _is_job_budget_fact_update(message):
        return ""
    if _strategy_revision_requested(message):
        return "strategy_revision"
    if _new_candidate_outreach_requested(message):
        return "new_candidate_outreach"
    if (
        any(token in message for token in ("人选", "候选人"))
        and any(token in message for token in ("补充", "补池", "找", "搜索", "搜", "寻访"))
    ):
        return "candidate_sourcing"
    rules = (
        ("memory_capture", ("记住", "别忘了", "记一下", "记住这个", "帮我记住")),
        ("job_archive", ("归档岗位", "岗位归档", "关闭岗位", "岗位关闭", "没拆分的岗位", "未拆分的岗位")),
        ("job_split", ("拆分岗位", "岗位拆分", "分成")),
        ("job_publish", ("发布岗位", "岗位发布", "上架岗位")),
        ("candidate_sourcing", ("补池", "寻访", "找人", "找些人选", "找候选人", "搜索人选")),
        ("candidate_outreach", ("触达", "开聊", "发送消息", "联系候选人", "二次跟进", "再跟一次", "催回复")),
        ("candidate_review", ("复核", "初筛", "停止推进", "继续推进", "过滤", "筛选", "名单", "重新过滤", "比较", "对比", "排序")),
        ("recommendation", ("推荐报告", "推荐给客户", "提交客户")),
        ("salary", ("谈薪", "薪资")),
    )
    for action, tokens in rules:
        if any(token in message for token in tokens):
            return action
    return ""


def _is_contextual_job_detail_message(message: str) -> bool:
    """Recognize compact job-detail replies when the job is already bound."""
    text = " ".join(str(message or "").split())
    if not text or _is_explicit_question(text):
        return False
    explicit_detail = any(
        marker in text
        for marker in ("补充岗位", "岗位细节", "岗位信息", "职位细节", "职位信息", "补充要求")
    )
    groups = (
        bool(re.search(r"(?:杭州|上海|苏州|北京|深圳|广州|南京|无锡|合肥|宁波|成都|武汉|西安|地点|工作地|base|坐标)", text, re.I)),
        "汇报" in text or "直属上级" in text or "汇报对象" in text,
        bool(re.search(r"\d+\s*年(?:以上|以内)?(?:经验|工作年限)?|经验|年限", text, re.I)),
        any(token in text for token in ("学历", "本科", "硕士", "博士", "专业")),
        any(token in text for token in ("技能", "技术栈", "行业", "背景", "职责", "团队", "下属", "出差", "英语")),
        any(token in text for token in ("必须", "优先", "最好", "可看", "不要", "排除", "接受")),
    )
    return explicit_detail and any(groups) or sum(bool(item) for item in groups) >= 2


def _plan_confirmation_reply(message: str) -> bool:
    """Only these exact short forms can confirm a previously presented plan."""
    compact = re.sub(r"[\s。.!！?？,，、]+", "", str(message or ""))
    return compact.lower() in {
        "好", "好的", "好了", "可以", "行", "嗯", "收到", "明白", "ok",
        "确认", "按这个来", "就这样", "开始", "开始吧", "执行", "继续",
    }


def _salary_plan_confirmation_reply(message: str) -> bool:
    """谈薪复述卡的确认：显式“确认创建”或既有短确认。"""
    compact = re.sub(r"[\s。.!！?？,，、]+", "", str(message or ""))
    return compact in {"确认创建", "确认创建计划", "确认创建谈薪计划", "确认创建该计划"} or _plan_confirmation_reply(message)


_SALARY_AMOUNT_RE = r"\d+(?:\.\d+)?\s*(?:w|W|万|k|K)"


def _salary_recap_amounts(facts: Any) -> tuple[str, str]:
    """从已记录的人选的薪资事实里取（当前薪资, 期望薪资）；没有则空串。"""
    quote = ""
    for item in reversed(facts) if isinstance(facts, list) else []:
        if (
            isinstance(item, dict)
            and not item.get("retracted")
            and str(item.get("kind") or "") == "candidate_compensation"
        ):
            quote = str(item.get("quote") or item.get("value") or "")
            break
    if not quote:
        return "", ""
    current = re.search(rf"(?:目前|现在|当前)[^，。；;]{{0,10}}?({_SALARY_AMOUNT_RE})", quote)
    expected = re.search(rf"(?:期望|预期)[^，。；;]{{0,10}}?({_SALARY_AMOUNT_RE})", quote)
    return (current.group(1) if current else "", expected.group(1) if expected else "")


def _deterministic_non_action_intent(
    message: str,
    selected: dict[str, Any],
    selected_facts: dict[str, Any],
    known_jobs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Build a no-action interpretation for unambiguous factual turns."""
    text = " ".join(str(message or "").split())
    if not text or _is_explicit_question(text):
        return None
    # These markers turn a fact into an explicit business command. Keep the
    # model/action gate for such turns, even when the same sentence contains a
    # salary or observation fact.
    if any(
        token in text
        for token in (
            "帮我", "请你", "麻烦", "整理", "生成", "制作", "处理", "创建", "新建", "建立",
            "更新岗位库", "写入岗位库", "保存到", "开始", "执行", "启动", "确认执行",
            "取消计划", "归档", "关闭岗位", "发布岗位", "上架岗位",
            "过滤", "筛选", "名单", "重新过滤", "输出名单", "给名单",
        )
    ) or re.search(r"(?:继续|恢复|重新|再).{0,8}(?:推进|寻访|搜索|找人|找候选人|触达|联系|复核|谈薪|过滤|筛选)", text, re.I):
        return None
    # 带明确动作语义的输入（“这个岗位再来一轮”）必须让位给动作/模型路径，
    # 不能被岗位细节事实路径吞掉。
    if re.search(r"(?:再来|再跑|再开|再做|重新来|重新跑|继续来)(?:一|新一)轮", text):
        return None

    contextual_job_detail = bool(
        _copilot_context_job_id(selected, selected_facts) is not None
        or len(known_jobs) == 1
    ) and _is_contextual_job_detail_message(text)
    initial_fact_updates = (
        [{"kind": "job_requirement", "quote": text, "value": text}]
        if contextual_job_detail
        else []
    )
    probe = enrich_turn_understanding(
        {
            "speech_act": "inform",
            "action": "none",
            "topic": "",
            "objective": "",
            "target": {"type": "global", "id": None, "client": "", "label": ""},
            "constraints": [],
            "fact_updates": initial_fact_updates,
            "action_evidence": [],
            "refers_to_previous": False,
            "confidence": 1.0,
            "needs_clarification": False,
        },
        message=text,
        pending_plan_ref={},
    )
    if any(token in text for token in ("匹配", "完美", "适合", "符合", "不合适")) and any(
        token in text for token in ("人选", "候选人", "他", "她")
    ):
        probe["topic"] = "candidate_match"
    fact_updates = [item for item in (probe.get("fact_updates") or []) if isinstance(item, dict)]
    if not fact_updates:
        return None
    kinds = {str(item.get("kind") or "") for item in fact_updates}
    job_fact = bool(kinds & {"job_budget", "job_requirement"})
    candidate_fact = bool(kinds & {"candidate_compensation", "candidate_availability", "candidate_preference"})
    if job_fact and _copilot_context_job_id(selected, selected_facts) is None and len(known_jobs) != 1:
        # Do not persist a job fact against global scope when several jobs are
        # possible; the caller should clarify the job first.
        return None
    if candidate_fact and str(selected.get("type") or "") != "candidate":
        return None
    target = {"type": "global", "id": None, "client": "", "label": ""}
    if str(selected.get("type") or "") in {"job", "candidate", "workflow"} and selected.get("id"):
        target = {"type": str(selected["type"]), "id": selected["id"], "client": "", "label": ""}
    elif len(known_jobs) == 1:
        item = known_jobs[0]
        target = {
            "type": "job",
            "id": item.get("id"),
            "client": str(item.get("client") or ""),
            "label": str(item.get("job") or ""),
        }
    return {
        **probe,
        "target": target,
        "source_message": text,
        "raw_constraint_changes": [],
    }


_JOB_REQUIREMENT_MARKERS = (
    "岗位需求", "JD", "jd", "职位描述", "岗位描述", "岗位职责", "岗位职则",
    "任职要求", "任职资格", "职位要求", "岗位要求", "招聘需求", "新增岗位",
    "新岗位", "录入岗位", "接入岗位", "岗位接入",
)


@staticmethod
def _is_job_requirement_message(message: str) -> bool:
    """检测用户是否正在输入/粘贴一份岗位需求（JD）。"""
    text = str(message or "")
    return any(marker in text for marker in _JOB_REQUIREMENT_MARKERS)


_COPILOT_SPEECH_ACTS = {
    "ask", "inform", "discuss", "propose", "confirm", "execute", "correct", "cancel", "other",
}
_COPILOT_SEMANTIC_ACTIONS = {
    "none", "candidate_sourcing", "strategy_revision", "candidate_outreach",
    "candidate_review", "job_publish", "job_split", "job_archive",
    "recommendation", "salary",
}
_COPILOT_CONSTRAINT_KINDS = {"must", "prefer", "allow", "exclude", "target_count", "other"}


def _is_plan_control_instruction(value: Any) -> bool:
    text = "".join(str(value or "").split())
    if not text:
        return False
    return any(
        token in text
        for token in (
            "先生成计划", "只生成计划", "先建立计划", "只建立计划",
            "先看计划", "先不要执行", "暂时不要执行", "不要执行",
            "先别执行", "不要启动", "先别启动",
        )
    )


def _is_explicit_question(message: str) -> bool:
    text = " ".join(str(message or "").split())
    return bool(re.search(r"[?？]", text)) or any(
        token in text
        for token in (
            "请问", "要不要", "是否", "能不能", "可不可以", "为什么", "怎么", "如何",
            "我是问", "我想问", "问一下", "想了解", "是什么", "怎么样",
        )
    )


# 公司知识库（CKB）直答：顾问问“XX公司是做什么的/公司背景/XX公司情况”时，
# 直接从公司知识库返回画像摘要；CKB 无画像时返回空串，走既有 LLM 路径（行为不变）。
_COMPANY_QUERY_LEADINS = (
    "我想了解下", "我想了解一下", "我想知道", "我想了解", "我想问", "请问",
    "介绍一下", "介绍下", "帮我看一下", "帮我看看", "帮我查一下", "帮我查查",
    "查一下", "查查", "看看", "了解一下", "了解下", "说说", "讲一下", "讲下",
    "你知道",
)
_COMPANY_QUERY_RE = re.compile(
    r"([一-龥A-Za-z0-9（）()·]{2,30}?)公司(?:是做什么的?|是干什么的?|是干嘛的?|做什么业务|怎么样|如何|什么情况|什么背景|的背景|的情况|的简介|背景|情况|简介)"
)
_COMPANY_QUERY_PLAIN_RE = re.compile(
    r"([一-龥A-Za-z0-9（）()·]{2,30}?)(?:是做什么的|是干什么的|是干嘛的)"
)
# 无“公司”字样的问法：“苏州迈为科技怎么样/迈为科技如何/芯源微什么情况”。
# 公司特征词后缀（长的在前）+ 评价/询问词；提取的是完整候选名，最终由 CKB 命中与否裁决。
_COMPANY_QUERY_BARE_RE = re.compile(
    r"([一-龥A-Za-z0-9（）()·]{2,30}?"
    r"(?:自动化|半导体|微电子|光电|光学|激光|精密|智能|科技|技术|电子|设备|装备|机电|材料|能源|医疗|通讯|通信|工业|机器|系统|股份|集团|实业|网络|信息|软件|芯片|集成|显示|器件|新材|制造|仪器|微纳|测控|数控|检测|微))"
    r"(?:怎么样|如何|什么情况|啥情况|的情况|的背景|怎么样呀|如何呀)"
)
# 纯短名公司：“倍福怎么样/ASMPT如何”——无特征词后缀，提取由 CKB 命中裁决（未命中走 LLM 回退，无害）
_COMPANY_QUERY_BARE_SHORT_RE = re.compile(
    r"([一-龥A-Za-z0-9]{2,6}?)(?:怎么样|如何|啥情况)"
)
# 代词/泛指词：命中即非公司查询（宁缺勿误伤对话）
_COMPANY_QUERY_STOP_TOKENS = (
    "岗位", "职位", "人选", "候选人", "简历", "项目", "需求", "流程", "方案",
    "这个", "那个", "这家", "那家", "哪家", "什么", "这块", "这种", "这些", "那些",
    "我们", "你们", "他们", "咱们", "咱", "贵司", "贵公司", "我司", "自家",
    "别人", "人家", "公司内部", "行业",
)


def _company_profile_query_name(message: str) -> str:
    """识别公司查询意图并提取公司名；非公司查询返回空串。

    提取只做候选名召回，最终是否有答案取决于 CKB 是否命中画像——未命中时
    调用方保持现状路径，因此这里宁宽勿漏。
    """
    text = " ".join(str(message or "").split())
    if not text:
        return ""
    match = (
        _COMPANY_QUERY_RE.search(text)
        or _COMPANY_QUERY_PLAIN_RE.search(text)
        or _COMPANY_QUERY_BARE_RE.search(text)
        or _COMPANY_QUERY_BARE_SHORT_RE.search(text)
    )
    if not match:
        return ""
    name = match.group(1).strip()
    # 剥掉常见引导语（“我想知道微导纳米公司怎么样” → 微导纳米）
    leadin_pattern = "|".join(re.escape(item) for item in _COMPANY_QUERY_LEADINS)
    name = re.sub(rf"^(?:{leadin_pattern})", "", name).strip("，,。.?？!！ ")
    if len(name) < 2 or any(token in name for token in _COMPANY_QUERY_STOP_TOKENS):
        return ""
    return name


def _format_company_profile_answer(name: str) -> str:
    """公司查询直答：返回 CKB 画像摘要；无画像返回空串（回退既有 LLM 路径）。"""
    profile = company_kb.get_profile(name)
    if not profile:
        return ""
    summary = company_kb.profile_summary(profile)
    if not summary:
        return ""
    lines = [
        f"## {profile['name']} 公司画像（公司知识库）",
        "",
        summary,
        "",
        f"证据 {profile['evidence_count']} 条 / 来源 {profile['source_count']} 个，置信度 {profile['confidence']:.2f}。",
    ]
    return "\n".join(lines)


# 人才情报直答：顾问问“XX方向薪酬水平/XX人才都在哪些公司”时，直接读生产库
# recalls 统计直答；库不可用/无样本时返回空串，走既有 LLM 路径（行为不变）。
_SALARY_QUERY_TOKENS = ("薪酬", "薪资", "待遇", "工资", "多少钱", "薪资水平", "收入")
# 指向具体人选/岗位上下文（谈薪、岗位预算）的提问不走对标直答
_SALARY_QUERY_STOP_TOKENS = ("这个", "我的", "候选人", "人选", "岗位薪酬")
_TALENT_MAP_QUERY_TOKENS = ("人才地图", "人都在哪", "人才", "分布", "哪些公司", "在哪", "的人")
_TALENT_MAP_QUERY_STOP_TOKENS = ("这个", "我的", "候选人", "人选")
# 方向词清理：剥掉疑问/通用修饰后，剩余即方向关键词（长的在前）
_INTEL_QUERY_NOISE_RE = re.compile(
    r"(?:怎么样|如何|是多少|多少|是什么|情况|水平|区间|范围|大概|大约|一般|"
    r"市面上|市场上|市场|现在|目前|请问|一下|给我|帮我|看看|查看|查查)"
)
_INTEL_QUERY_SUFFIX_RE = re.compile(
    r"(?:方向|岗位|职位|技术|工程师|行业|领域|背景|的|都|也|呢|啊|呀|吧|在|有|人|里|做|是)"
)
# 方向词前缀动词（“做薄膜沉积的/查一下运动控制”）→ 剥掉
_INTEL_QUERY_PREFIX_RE = re.compile(r"^(?:做|搞|找|找找|查|查查|看|看看|搜|搜搜|挖|挖挖|招|招招)")


def _extract_direction_keyword(text: str, token_pattern: str) -> str:
    """剥掉引导语/意图词/疑问修饰，提取方向关键词；不足 2 字返回空串。"""
    kw = text
    leadin_pattern = "|".join(re.escape(item) for item in _COMPANY_QUERY_LEADINS)
    kw = re.sub(rf"^(?:{leadin_pattern})", "", kw)
    kw = re.sub(token_pattern, " ", kw)
    kw = _INTEL_QUERY_NOISE_RE.sub(" ", kw)
    kw = _INTEL_QUERY_SUFFIX_RE.sub(" ", kw)
    # 剥前缀动词（循环剥, 如“做薄膜沉积”→“薄膜沉积”）
    while True:
        new = _INTEL_QUERY_PREFIX_RE.sub("", kw)
        if new == kw:
            break
        kw = new
    kw = re.sub(r"[\s?？!！。,.，、：:；;]+", "", kw)
    return kw if len(kw) >= 2 else ""


def _salary_query(message: str) -> str:
    """识别薪酬对标提问并提取方向关键词；非方向性薪酬提问返回空串。

    “运动控制方向薪酬水平怎么样” → “运动控制”；
    “这个候选人的期望薪资”/“岗位薪酬预算”等指向具体人选/岗位的返回空串，
    交给既有谈薪/事实路径处理。
    """
    text = " ".join(str(message or "").split())
    if not text or any(token in text for token in _SALARY_QUERY_STOP_TOKENS):
        return ""
    if not any(token in text for token in _SALARY_QUERY_TOKENS):
        return ""
    return _extract_direction_keyword(text, r"(?:薪资水平|薪酬|薪资|待遇|工资|多少钱|收入)")


def _talent_map_query(message: str) -> str:
    """识别人才地图提问并提取方向关键词；非方向性提问返回空串。

    “薄膜沉积的人才都在哪些公司” → “薄膜沉积”。
    """
    text = " ".join(str(message or "").split())
    if not text or any(token in text for token in _TALENT_MAP_QUERY_STOP_TOKENS):
        return ""
    if not any(token in text for token in _TALENT_MAP_QUERY_TOKENS):
        return ""
    return _extract_direction_keyword(
        text,
        r"(?:人才地图|人都在哪|人都去哪|在哪些公司|哪些公司|在哪些|哪些|人才|分布|在哪|哪里|哪儿|集中|公司)",
    )


def _format_salary_benchmark_answer(kw: str) -> str:
    """薪酬对标直答：返回样本统计；无样本/库不可用返回空串（回退既有 LLM 路径）。"""
    data = talent_intel.salary_benchmark(kw)
    if not data:
        return ""
    lines = [f"== {kw} 薪酬对标（{data['n']} 样本）=="]
    lines.append(
        f"月薪 P25 {data['p25']:.0f}k / 中位 {data['p50']:.0f}k / P75 {data['p75']:.0f}k"
        f"（区间 {data['min']:.0f}–{data['max']:.0f}k），年化约 {data['annual_万']:.0f} 万"
    )
    companies = data.get("companies") or []
    if companies:
        lines.append("样本公司：" + "、".join(f"{name}({cnt})" for name, cnt in companies[:8]))
    return "\n".join(lines)


def _format_talent_map_answer(kw: str) -> str:
    """人才地图直答：返回目标公司列表；无目标公司/库不可用返回空串（回退既有 LLM 路径）。"""
    data = talent_intel.talent_map(kw)
    if not data or not data.get("companies"):
        return ""
    lines = [f"== {kw} 人才地图（{len(data['companies'])} 家公司，{data['total']} 人）=="]
    for item in data["companies"][:15]:
        seg = f"- {item['name']}：{item['n']} 人"
        details: list[str] = []
        if item.get("exp_median") is not None:
            details.append(f"经验中位 {item['exp_median']:.0f} 年")
        edu = item.get("edu") or []
        if edu:
            details.append("学历 " + "、".join(f"{name}{cnt}" for name, cnt in edu[:3]))
        city = item.get("city") or []
        if city:
            details.append("城市 " + "、".join(f"{name}({cnt})" for name, cnt in city[:3]))
        if item.get("salary_min") is not None and item.get("salary_max") is not None:
            details.append(f"月薪 {item['salary_min']:.0f}–{item['salary_max']:.0f}k")
        if details:
            seg += "（" + "；".join(details) + "）"
        lines.append(seg)
    return "\n".join(lines)


# 查询型名单请求：顾问直接要"名单/列表/筛出人选"，应当直答候选池，
# 而不是建一个等待确认的执行计划（2026-08-10 长越机械人选名单卡在 create_plan）。
_QUERY_LIST_MARKERS = (
    "名单", "列表", "列一下", "列出", "列出来", "筛出", "筛一下", "筛选",
    "有哪些人选", "有什么人选", "给一份", "整理一份", "排一下", "排个序",
    "优先评估", "优先名单", "核验名单", "比较", "对比",
)
_QUERY_LIST_EXCLUSIONS = (
    "寻访", "补池", "找人", "找候选人", "搜索", "搜人", "触达", "开聊",
    "发送", "联系候选人", "更新", "拆分", "归档", "发布", "推荐给客户",
    "提交客户", "谈薪", "复核进度", "计划", "执行", "启动", "开始",
    "发给客户", "发给", "重新评估", "评估进度", "风险", "问题",
)


_PLAIN_QUERY_MARKERS = (
    "哪些", "几个", "多少", "什么岗位", "岗位情况", "岗位列表", "岗位状态",
    "情况如何", "进展", "看看", "查一下", "有没有", "有哪", "怎么样", "如何",
    "几个岗位", "在招", "招聘中", "招什么",
)
_PLAIN_QUERY_ACTION_MARKERS = (
    "寻访", "补池", "补人", "补", "找人", "找候选人", "触达", "开聊", "发送", "联系",
    "谈薪", "创建", "启动", "执行", "发布", "归档", "更新", "拆分", "推荐给",
    "提交", "发给", "预算", "薪资", "薪酬", "期望", "到岗", "意向", "匹配",
    "复核", "过滤", "筛选", "名单", "评估", "推进",
)


def _is_plain_query(message: str) -> bool:
    """判断消息是否为纯查询（不需要唯一岗位归因即可回答）。

    含执行/事实归因词（补人、触达、谈薪、预算…）的消息一律不是纯查询；
    其余：疑问句、候选名单请求、或含岗位/客户查询词的消息视为纯查询，
    歧义守卫放行给 LLM，LLM 列出多岗位/多客户即可。
    """
    text = " ".join(str(message or "").split())
    if not text:
        return False
    if any(marker in text for marker in _PLAIN_QUERY_ACTION_MARKERS):
        return False
    if _is_explicit_question(text):
        return True
    if _is_candidate_list_query(text):
        return True
    return any(marker in text for marker in _PLAIN_QUERY_MARKERS)


def _is_candidate_list_query(message: str) -> bool:
    text = " ".join(str(message or "").split())
    if _is_explicit_question(text):
        return False
    # “禁挖名单/目标公司名单/排除名单”是岗位策略事实，不是候选人名单请求。
    # 先移除这些政策语境，再判断是否仍有明确的候选池列表动作，避免把锚点
    # 回答提前拦截成“候选池为空”。
    candidate_list_text = re.sub(
        r"(?:禁挖|排除|竞业|目标公司|目标企业|客户|黑|白)名单",
        "",
        text,
        flags=re.IGNORECASE,
    )
    if not any(marker in candidate_list_text for marker in _QUERY_LIST_MARKERS):
        return False
    if any(token in candidate_list_text for token in _QUERY_LIST_EXCLUSIONS):
        return False
    return True


def _requests_grade_filter(message: str) -> bool:
    """判断消息是否请求“分级过滤”（按硬性证据输出 A/B/C 名单）。

    与普通名单直答的区别：含“过滤/分级/匹配度/按证据/按硬性门槛/筛一下再给”
    等明确分级意图时升级为 candidate_pool_filter；仅“给名单/列一下”
    维持普通候选名单。

    2026-08-13 修复：原正则要求“过滤…名单”落在 20 字窗口内，导致
    “把所有候选人过滤一下，再看下匹配度，不匹配的就停止推进，然后再给我名单”
    这类长句被漏判为普通名单。改为“分级意图词 + 名单意图词”组合判定，
    距离无关，同时保留原有精确 token 兜底。
    """
    text = " ".join(str(message or "").split())
    if not text or _is_explicit_question(text):
        return False
    grade_tokens = (
        "过滤", "筛选", "分级", "分层", "重新过滤", "再筛", "筛一下",
        "匹配度", "按硬性", "按证据", "不匹配",
    )
    list_tokens = ("名单", "列表", "给我", "输出")
    if any(token in text for token in grade_tokens) and any(token in text for token in list_tokens):
        return True
    return any(token in text for token in ("分级过滤", "筛选出", "过滤出", "按匹配度"))


_BATCH_STOP_MARKERS = (
    "停止推进", "停止", "停掉", "不匹配的就停止", "不匹配的停止",
    "不匹配的停掉", "筛掉不匹配", "把不匹配的停", "淘汰",
)


def _requests_batch_stop(message: str) -> bool:
    """判断消息是否为“分级过滤 + 停止推进”的批量落库指令。

    必须在 _requests_grade_filter 之上叠加明确的停止措辞，避免普通“给名单”
    被误判成批量写库。该函数只做意图判定，不执行写库。
    """
    if not _requests_grade_filter(message):
        return False
    text = " ".join(str(message or "").split())
    return any(marker in text for marker in _BATCH_STOP_MARKERS)


def _format_candidate_list_answer(db_path: str, job_id: int, message: str) -> str:
    """从候选池生成岗位名单文本（含岗位上下文、阶段分组、固晶/共晶/键合优先）。"""
    return _build_candidate_list_card(db_path, job_id, message)[0]


def _build_candidate_list_card(db_path: str, job_id: int, message: str) -> tuple[str, dict[str, Any]]:
    """生成名单文本 + 结构化卡片（action_card，前端渲染可点击名单弹窗）。

    返回 (answer_text, card)。card 形如：
    {
      "type": "candidate_list",
      "title": "长越科技｜机械高级工程师（岗位 137）候选名单",
      "context": {"type": "job", "id": 137},
      "summary": {"total": 329, "active": 321, "stopped": 8, "bonder_count": 37},
      "groups": [
        {"key": "bonder", "label": "固晶机/共晶机/键合机背景", "priority": true, "candidates": [...]},
        {"key": "active", "label": "其余可推进候选", "priority": false, "candidates": [...]},
        {"key": "stopped", "label": "已停止推进", "priority": false, "candidates": [...]},
      ],
    }
    每个 candidate: {id, name, company, title, stage, flow_bucket}
    """
    import sqlite3
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        job = conn.execute(
            """
            SELECT j.id, c.name AS client, j.title
            FROM jobs j JOIN clients c ON c.id = j.client_id
            WHERE j.id = ?
            """,
            (job_id,),
        ).fetchone()
        if not job:
            return "", {}
        rows = conn.execute(
            """
            SELECT jc.id AS jc_id, p.display_name, p.current_company, p.current_title,
                   jc.clean_stage, jc.flow_bucket
            FROM job_candidates jc
            LEFT JOIN people p ON p.id = jc.person_id
            WHERE jc.job_id = ?
            ORDER BY jc.id DESC
            """,
            (job_id,),
        ).fetchall()
        if not rows:
            empty_text = f"结论：岗位「{job['client']}｜{job['title']}」当前候选池为空。\n\n下一步：需要先启动一轮寻访补池。"
            card = {
                "type": "candidate_list",
                "title": f"{job['client']}｜{job['title']}（岗位 {job['id']}）候选名单",
                "context": {"type": "job", "id": job["id"]},
                "summary": {"total": 0, "active": 0, "stopped": 0, "bonder_count": 0},
                "groups": [],
            }
            return empty_text, card
        # 固晶/共晶/键合 关键词优先标记：一次性 JOIN 避免 N+1，candidate_profiles
        # 缺表时降级为普通名单（老库无此表，直接抛错会导致 copilot 500）。
        bonder = any(token in message for token in ("固晶", "共晶", "键合"))
        bonder_ids: set[int] = set()
        if bonder and _table_exists(conn, "candidate_profiles"):
            try:
                prof_rows = conn.execute(
                    """
                    SELECT DISTINCT jc.id AS jc_id
                    FROM candidate_profiles cp
                    JOIN people p ON p.display_name = cp.candidate_name
                                 AND p.current_company = cp.candidate_company
                    JOIN job_candidates jc ON jc.person_id = p.id
                    WHERE jc.job_id = ?
                      AND (cp.profile_summary LIKE '%固晶%'
                        OR cp.profile_summary LIKE '%共晶%'
                        OR cp.profile_summary LIKE '%键合%')
                      AND cp.profile_summary NOT LIKE '%补搜%'
                    """,
                    (job_id,),
                ).fetchall()
                bonder_ids = {int(r["jc_id"]) for r in prof_rows}
            except sqlite3.Error:
                bonder_ids = set()
        stage_order = {
            "待复核": 0, "新增寻访": 1, "已触达": 2, "已联系": 3,
            "初筛不通过": 4, "停止": 4, "淘汰": 4, "关闭": 4, "最近寻访": 4,
        }
        def stage_rank(stage: str) -> int:
            for key, rank in stage_order.items():
                if key in (stage or ""):
                    return rank
            return 3
        _STOP_TOKENS = ("初筛不通过", "停止", "淘汰", "关闭")
        rows = sorted(rows, key=lambda r: (stage_rank(str(r["clean_stage"])), r["jc_id"]))
        active = [r for r in rows if not any(k in (r["clean_stage"] or "") for k in _STOP_TOKENS)]
        stopped = [r for r in rows if any(k in (r["clean_stage"] or "") for k in _STOP_TOKENS)]
        # 优先名单：固晶/共晶/键合 命中者（未停止），按入库顺序排列
        prioritized = [r for r in active if r["jc_id"] in bonder_ids]
        prioritized.sort(key=lambda r: r["jc_id"])
        other_active = [r for r in active if r["jc_id"] not in bonder_ids]

        def to_candidate(r) -> dict[str, Any]:
            return {
                "id": int(r["jc_id"]),
                "name": str(r["display_name"] or "未知"),
                "company": str(r["current_company"] or ""),
                "title": str(r["current_title"] or ""),
                "stage": str(r["clean_stage"] or ""),
                "flow_bucket": str(r["flow_bucket"] or ""),
            }

        groups: list[dict[str, Any]] = []
        if prioritized:
            groups.append({
                "key": "bonder", "label": "固晶机/共晶机/键合机背景",
                "priority": True, "candidates": [to_candidate(r) for r in prioritized],
            })
        if other_active:
            groups.append({
                "key": "active", "label": "其余可推进候选",
                "priority": False, "candidates": [to_candidate(r) for r in other_active],
            })
        if stopped:
            groups.append({
                "key": "stopped", "label": "已停止推进",
                "priority": False, "candidates": [to_candidate(r) for r in stopped],
            })

        def fmt(r) -> str:
            stage = str(r["clean_stage"] or "")
            parts = [str(r["display_name"] or "未知")]
            if r["current_company"]:
                parts.append(str(r["current_company"]))
            if r["current_title"]:
                parts.append(str(r["current_title"]))
            label = " | ".join(dict.fromkeys(parts))
            return f"- {label}（{stage}）" if stage else f"- {label}"

        lines: list[str] = []
        lines.append(f"## {job['client']}｜{job['title']}（岗位 {job['id']}）候选名单")
        lines.append(f"共 {len(rows)} 人，其中可推进 {len(active)} 人、已停止 {len(stopped)} 人。\n")
        # 固晶优先组的优先级标注：A 级=直接固晶机/键合机经验，B 级=封装/精密设备相关，C 级=其余命中
        priority_notes: dict[int, tuple[str, str]] = {}
        if prioritized and bonder:
            try:
                for pr in conn.execute(
                    """
                    SELECT jc.id AS jc_id, cp.profile_summary
                    FROM job_candidates jc
                    JOIN people p ON p.id = jc.person_id
                    JOIN candidate_profiles cp ON cp.candidate_name = p.display_name
                                            AND cp.candidate_company = p.current_company
                    WHERE jc.job_id = ? AND jc.id IN (%s)
                      AND cp.profile_summary NOT LIKE '%%补搜%%'
                    """
                    % ",".join("?" * len(prioritized)),
                    (job_id, *[r["jc_id"] for r in prioritized]),
                ).fetchall():
                    summary = str(pr["profile_summary"] or "")
                    jc_id = int(pr["jc_id"])
                    if any(t in summary for t in ("固晶机", "固晶", "键合机", "die bond", "wire bond", "共晶机")):
                        level = "A"
                    elif any(t in summary for t in ("封装", "ASMPT", "先进微电子", "精密设备", "光刻", "刻蚀", "CVD", "PVD", "真空设备")):
                        level = "B"
                    else:
                        level = "C"
                    note = " ".join(summary.split())[:64]
                    priority_notes[jc_id] = (level, note)
            except sqlite3.Error:
                priority_notes = {}
        if prioritized:
            lines.append(f"### ⭐ 固晶机/共晶机/键合机背景（优先评估，{len(prioritized)} 人）")
            for r in prioritized[:12]:
                level, note = priority_notes.get(int(r["jc_id"]), ("C", ""))
                base = fmt(r)
                if level in ("A", "B"):
                    lines.append(f"- **【{level}级】** {base[2:]} — {note}")
                else:
                    lines.append(base)
            lines.append("")
        if other_active:
            lines.append(f"### 其余可推进候选（{len(other_active)} 人，列前 15）")
            lines.extend(fmt(r) for r in other_active[:15])
            lines.append("")
        if stopped:
            lines.append(f"### 已停止推进（{len(stopped)} 人，列前 5）")
            lines.extend(fmt(r) for r in stopped[:5])
        card = {
            "type": "candidate_list",
            "title": f"{job['client']}｜{job['title']}（岗位 {job['id']}）候选名单",
            "context": {"type": "job", "id": job["id"]},
            "summary": {
                "total": len(rows), "active": len(active), "stopped": len(stopped),
                "bonder_count": len(prioritized),
            },
            "groups": groups,
        }
        return "\n".join(lines), card
    finally:
        conn.close()


def _is_candidate_list_composition_question(message: str) -> bool:
    """判断消息是否为“质疑名单构成/来源”的提问（如“怎么都是做光刻机的”）。

    这类提问应回答构成分析与原因，而不是再次输出名单。
    """
    text = " ".join(str(message or "").split())
    if not _is_explicit_question(text):
        return False
    if not any(marker in text for marker in ("名单", "列表", "这些", "这批", "候选")):
        return False
    # 质疑/惊讶语气的核心特征：怎么/为什么/为何 + 都是/全是/都做/没有/找不到
    if not re.search(r"(?:怎么|为什么|为何).*(?:都是|全是|都做|都是做|都来自|都集中|没有|没看到|看不到|找不到)", text):
        return False
    return True


def _build_candidate_list_composition_answer(db_path: str, job_id: int, message: str) -> str:
    """生成“名单构成分析”回答：按公司/行业统计分布，解释为什么名单偏某类背景。"""
    import sqlite3
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        job = conn.execute(
            """
            SELECT j.id, c.name AS client, j.title
            FROM jobs j JOIN clients c ON c.id = j.client_id
            WHERE j.id = ?
            """,
            (job_id,),
        ).fetchone()
        if not job:
            return ""
        rows = conn.execute(
            """
            SELECT p.current_company AS company, p.current_title AS title
            FROM job_candidates jc
            LEFT JOIN people p ON p.id = jc.person_id
            WHERE jc.job_id = ?
            """,
            (job_id,),
        ).fetchall()
        if not rows:
            return ""
        # 按公司聚合
        company_counts: dict[str, int] = {}
        for r in rows:
            company = str(r["company"] or "未知公司").strip()
            if not company or company in ("候选人目前没有工作", "我还不知道候选人在哪家公司工作"):
                company = "（未标注公司）"
            company_counts[company] = company_counts.get(company, 0) + 1
        top_companies = sorted(company_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
        total = len(rows)
        # 半导体/光刻机相关关键词
        semicon_keywords = (
            "光刻", "微电子", "半导体", "精科", "北方华创", "中微", "华海清科",
            "上海微电子", "晶盛", "长川", "天准", "华兴源创", "精测", "第四十五",
            "芯", "纳", "宏", "AMAT", "ASML", "SMEE", "NAURA", "屹唐", "拓荆",
        )
        semicon_hits = sum(
            1 for r in rows if str(r["company"] or "") and any(k in str(r["company"]) for k in semicon_keywords)
        )
        semicon_ratio = semicon_hits / total * 100 if total else 0
        lines: list[str] = []
        lines.append(f"## {job['client']}｜{job['title']}（岗位 {job['id']}）名单构成分析")
        lines.append(f"共 {total} 人。这不是巧合——名单构成主要来自当前岗位的寻访策略。\n")
        lines.append(f"### 公司分布（前 {len(top_companies)}）")
        for name, count in top_companies:
            lines.append(f"- {name}：{count} 人")
        lines.append("")
        if semicon_hits:
            lines.append(
                f"### 原因\n"
                f"名单中约 {semicon_hits}/{total} 人（{semicon_ratio:.0f}%）来自半导体/光刻设备相关公司。"
                f"原因是岗位「{job['title']}」的寻访策略把目标公司池和关键词集中在了半导体设备厂商"
                f"（光刻机、量测、刻蚀、CVD 等），机械工程师背景的候选人也因此以这些公司为主。"
            )
        else:
            lines.append("### 原因\n当前名单没有明显行业集中，以上是公司分布参考。")
        lines.append(
            "\n### 下一步\n"
            "如果你想看到更分散的行业构成（例如通用机械、3C 自动化、光伏设备等），"
            "告诉我目标方向，我可以按新方向重新筛名单。"
        )
        return "\n".join(lines)
    finally:
        conn.close()


def _verbatim_constraint_candidates(messages: list[str]) -> list[dict[str, str]]:
    """Extract auditable clauses without normalizing the consultant's terminology."""
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for message in messages:
        for raw_clause in re.split(r"[，,；;。\n]+", str(message or "")):
            clause = raw_clause.strip()
            if not clause or len(clause) > 180 or _is_plan_control_instruction(clause):
                continue
            kind = ""
            if any(token in clause for token in ("必须", "一定要", "硬性", "不能少")):
                kind = "must"
            elif any(token in clause for token in ("优先", "更好", "最好")):
                kind = "prefer"
            elif any(token in clause for token in ("可看", "可以看", "可接受")):
                kind = "allow"
            elif any(token in clause for token in ("排除", "不要", "不能要", "不考虑")):
                kind = "exclude"
            elif (
                re.search(r"\d+\s*(?:位|个|名|人)(?:选|候选人)?", clause)
                and not re.search(r"(?:只|已|已经|目前|现在).*找到", clause)
            ):
                kind = "target_count"
            elif any(token in clause for token in ("年经验", "年以上", "职级", "行业", "方向")):
                kind = "other"
            if not kind or clause in seen:
                continue
            seen.add(clause)
            rows.append({"quote": clause, "kind": kind})
    return rows[-12:]


def _interpret_copilot_message(
    self,
    message: str,
    selected: dict[str, Any],
    selected_facts: dict[str, Any],
    existing_focus: dict[str, Any] | None,
    conversation_history: list[dict[str, str]],
    last_assistant_message: str,
    confirmation_plan_ref: dict[str, Any] | None = None,
    uploaded_attachment_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Use the model for semantics, then constrain its output to verified local facts."""
    deterministic_action = self._copilot_action_kind(message) or "none"
    plan_reply = _plan_confirmation_reply(message)
    turn_pending_plan = (
        dict(confirmation_plan_ref or {})
        if plan_reply
        else dict((existing_focus or {}).get("pending_workflow") or {})
    )
    recent_user_messages = [
        str(item.get("content") or "")
        for item in conversation_history[-16:]
        if item.get("role") == "user"
    ]
    known_jobs = _jobs_relevant_to_selected_context(
        self._mentioned_jobs_for_copilot(message),
        selected,
        selected_facts,
        message,
    )
    current_job = selected_facts.get("job") if isinstance(selected_facts.get("job"), dict) else {}
    focus_job = existing_focus.get("job") if isinstance(existing_focus, dict) and isinstance(existing_focus.get("job"), dict) else {}
    known_targets: list[dict[str, Any]] = []
    for item in [current_job, focus_job, *known_jobs]:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        target = {
            "type": "job", "id": int(item["id"]),
            "client": str(item.get("client") or selected_facts.get("client") or (existing_focus or {}).get("client") or ""),
            "label": str(item.get("title") or item.get("job") or ""),
        }
        if target not in known_targets:
            known_targets.append(target)
    if selected.get("type") in {"candidate", "workflow"} and selected.get("id"):
        known_targets.append({
            "type": selected["type"], "id": selected["id"],
            "client": str(selected_facts.get("client") or ""),
            "label": str((selected_facts.get("candidate") or {}).get("name") or (selected_facts.get("workflow") or {}).get("title") or ""),
        })
    payload = {
        "current_message": message,
        "recent_user_messages": recent_user_messages[-8:],
        "last_assistant_message": last_assistant_message[-1200:],
        "current_context": selected,
        "known_targets": known_targets,
        "pending_action": {
            "action": str((existing_focus or {}).get("action") or "none"),
            "objective": str((existing_focus or {}).get("objective") or ""),
            "constraints": list(
                (existing_focus or {}).get("constraint_ledger")
                or (existing_focus or {}).get("constraints")
                or []
            ),
            "pending_plan": turn_pending_plan,
            "confirmation_anchor": bool(plan_reply and turn_pending_plan.get("workflow_id")),
        },
        "conversation_state": dict((existing_focus or {}).get("conversation_state") or {}),
        "deterministic_hint": deterministic_action,
    }
    # 上传附件摘要：顾问常用“这个人选/这份简历”指代刚上传的简历附件，
    # 意图层需要附件内容才能解析指代。附件内容不可信，仅作指代解析依据；
    # 正文截断后仍会过 sanitize_payload 脱敏（姓名等业务信息保留）。
    attachment_summaries: list[dict[str, Any]] = []
    for item in ((uploaded_attachment_evidence or {}).get("items") or [])[:2]:
        if not isinstance(item, dict):
            continue
        attachment_summaries.append({
            "file_name": str(item.get("file_name") or "")[:180],
            "text_excerpt": str(item.get("extracted_text") or "")[:4000],
            "untrusted_document_content": True,
        })
    if attachment_summaries:
        payload["uploaded_attachments"] = attachment_summaries
    raw = _deterministic_non_action_intent(message, selected, selected_facts, known_jobs) or {}
    if not raw and not plan_reply:
        try:
            interpreted = self.llm.interpret_copilot_intent(sanitize_payload(payload))
            if isinstance(interpreted, dict):
                raw = interpreted
        except (LLMError, ValueError, TypeError):
            raw = {}

    if plan_reply:
        # A vague confirmation is safe only when the immediately preceding
        # assistant turn presented this exact, still-planned workflow. Do not
        # let a permissive model recover an older focus plan after an
        # intervening factual turn.
        if not turn_pending_plan.get("workflow_id"):
            raw = {
                "speech_act": "other",
                "action": "none",
                "topic": "workflow",
                "objective": "",
                "target": {"type": "global", "id": None, "client": "", "label": ""},
                "constraints": [],
                "fact_updates": [],
                "action_evidence": [],
                "refers_to_previous": False,
                "confidence": 1.0,
                "needs_clarification": True,
                "missing_fields": ["要确认的最新计划"],
                "clarification_question": "请先确认上一条仍待执行的具体计划。",
            }
        else:
            raw = dict(raw or {})
            raw["speech_act"] = "confirm"
            raw["action"] = str(
                raw.get("action")
                or turn_pending_plan.get("action")
                or (existing_focus or {}).get("action")
                or "none"
            )
            raw["refers_to_previous"] = True
            raw["confidence"] = max(float(raw.get("confidence") or 0.0), 0.95)
            raw["needs_clarification"] = False
            raw["action_evidence"] = [message]
            if isinstance(turn_pending_plan.get("target"), dict):
                raw["target"] = dict(turn_pending_plan["target"])

    speech_act = str(raw.get("speech_act") or "").strip().lower()
    if _is_explicit_question(message):
        speech_act = "ask"
    elif speech_act not in _COPILOT_SPEECH_ACTS:
        if plan_reply and turn_pending_plan.get("workflow_id"):
            speech_act = "confirm"
        elif _is_short_ack(message) and (existing_focus or {}).get("action"):
            speech_act = "confirm"
        elif (
            (existing_focus or {}).get("action") in {"candidate_sourcing", "strategy_revision"}
            and re.fullmatch(r"(?:可以|确认|现在)?(?:开始|继续|重新|执行)?(?:搜索|寻访)(?:吧|了|可以)?", message, re.I)
        ):
            speech_act = "confirm"
        elif re.search(r"(?:取消|算了|停止这个计划|不要执行)", message):
            speech_act = "cancel"
        elif re.search(r"(?:纠正|更正|不是.+而是|改成|改为|去掉|删除|不再要求)", message):
            speech_act = "correct"
        elif deterministic_action != "none":
            speech_act = "execute" if re.search(r"(?:立即|马上|直接|现在)?(?:开始|执行)", message) else "propose"
        else:
            speech_act = "other"
    action = str(raw.get("action") or "").strip().lower()
    if action not in _COPILOT_SEMANTIC_ACTIONS:
        action = "none"
    if action == "none" and deterministic_action != "none":
        action = deterministic_action
    if action == "new_candidate_outreach":
        action = "candidate_sourcing"
    job_budget_fact_update = _is_job_budget_fact_update(message)
    if job_budget_fact_update and action == "salary":
        action = "none"
        if speech_act in {"propose", "confirm", "execute", "correct"}:
            speech_act = "inform"
    if (
        action == "none"
        and speech_act in {"confirm", "correct", "cancel"}
        and (existing_focus or {}).get("action")
        and (not plan_reply or turn_pending_plan.get("workflow_id"))
    ):
        previous_action = str((existing_focus or {}).get("action") or "none")
        action = previous_action if previous_action in _COPILOT_SEMANTIC_ACTIONS else "none"
    refinement_mode = _pending_sourcing_refinement_mode(message, existing_focus, speech_act)
    if refinement_mode:
        action = "strategy_revision"
        speech_act = "correct" if refinement_mode == "revise" else "discuss"

    allowed_target_keys = {(str(item.get("type")), str(item.get("id"))): item for item in known_targets}
    raw_target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
    target = allowed_target_keys.get((str(raw_target.get("type") or ""), str(raw_target.get("id") or "")))
    if target is None and selected.get("type") in {"job", "candidate", "workflow"} and selected.get("id"):
        target = allowed_target_keys.get((str(selected["type"]), str(selected["id"])))
    if target is None and bool(raw.get("refers_to_previous")) and len(known_targets) == 1:
        target = known_targets[0]
    target = dict(target or {"type": "global", "id": None, "client": "", "label": ""})

    source_messages = [*recent_user_messages, message]
    source_corpus = "\n".join(source_messages)
    constraints: list[dict[str, str]] = []
    seen_quotes: set[str] = set()
    # Questions and discussion can quote a possible constraint without adopting
    # it. Keep those turns read-only so phrases such as "要不要继续寻访" do not
    # become an exclusion in the condition ledger.
    constraint_inputs = [item for item in source_messages if not _is_explicit_question(item)]
    model_constraints = [] if speech_act in {"ask", "discuss"} else (raw.get("constraints") or [])
    for item in model_constraints:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("quote") or "").strip()
        kind = str(item.get("kind") or "other").strip().lower()
        if quote and quote in source_corpus and quote not in seen_quotes and not _is_plan_control_instruction(quote):
            seen_quotes.add(quote)
            constraints.append({"quote": quote, "kind": kind if kind in _COPILOT_CONSTRAINT_KINDS else "other"})
    for item in _verbatim_constraint_candidates(constraint_inputs[-8:]):
        if item["quote"] not in seen_quotes:
            seen_quotes.add(item["quote"])
            constraints.append(item)

    try:
        confidence = max(0.0, min(float(raw.get("confidence") or 0.0), 1.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if not raw and deterministic_action != "none":
        confidence = 0.8
    elif not raw and speech_act in {"confirm", "correct", "cancel"} and (existing_focus or {}).get("action"):
        confidence = max(0.8, min(float((existing_focus or {}).get("confidence") or 0.0), 1.0))
    missing_fields = [str(item).strip()[:80] for item in (raw.get("missing_fields") or []) if str(item).strip()][:6]
    needs_clarification = bool(raw.get("needs_clarification"))
    if plan_reply and action == "none":
        needs_clarification = True
        missing_fields = missing_fields or ["要确认的动作"]
    raw_constraint_changes = [
        item
        for item in (raw.get("constraint_changes") or [])
        if isinstance(item, dict)
        and not _is_plan_control_instruction(item.get("quote") or item.get("value"))
    ] if speech_act not in {"ask", "discuss", "other"} else []
    understanding = {
        "version": "copilot_understanding_v1",
        "speech_act": speech_act,
        "action": action,
        "topic": str(raw.get("topic") or "").strip()[:48],
        "objective": str(raw.get("objective") or ((existing_focus or {}).get("objective") if speech_act == "confirm" else "") or "").strip()[:500],
        "target": target,
        "constraints": constraints[-12:],
        "fact_updates": list(raw.get("fact_updates") or [])[:8],
        "action_evidence": list(raw.get("action_evidence") or [])[:4],
        "refers_to_previous": bool(raw.get("refers_to_previous")) or speech_act == "confirm",
        "confidence": round(confidence, 3),
        "needs_clarification": needs_clarification,
        "missing_fields": missing_fields,
        "clarification_question": str(raw.get("clarification_question") or "").strip()[:240],
        "source_message": message,
        "raw_constraint_changes": raw_constraint_changes,
        "safe_for_action": bool(
            action != "none"
            and speech_act in {"propose", "confirm", "execute", "correct", "cancel"}
            and confidence >= 0.72
            and not needs_clarification
        ),
    }
    return enrich_turn_understanding(
        understanding,
        message=message,
        pending_plan_ref=turn_pending_plan,
    )


def _latest_assistant_plan_anchor(self, session_id: str) -> dict[str, Any]:
    """Read only the immediately preceding assistant plan presentation."""
    session_id = str(session_id or "").strip()
    if not session_id:
        return {}
    conn = self._connect()
    try:
        row = conn.execute(
            """
            SELECT structured_json FROM agent_copilot_messages
            WHERE session_id=? AND role='assistant'
            ORDER BY id DESC LIMIT 1
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    structured = _loads(row["structured_json"], {}) if row else {}
    anchor = structured.get("presented_plan_ref") if isinstance(structured, dict) else {}
    return dict(anchor) if isinstance(anchor, dict) and anchor.get("workflow_id") else {}


def _latest_assistant_plan_confirmation(self, session_id: str) -> dict[str, Any]:
    """Read the immediately preceding assistant pre-creation recap card (复述确认中间态).

    与 _latest_assistant_plan_anchor 同通道：卡片存在 assistant structured_json 的
    pending_plan_confirmation 字段，只对紧邻的下一条用户消息生效。
    """
    session_id = str(session_id or "").strip()
    if not session_id:
        return {}
    conn = self._connect()
    try:
        row = conn.execute(
            """
            SELECT structured_json FROM agent_copilot_messages
            WHERE session_id=? AND role='assistant'
            ORDER BY id DESC LIMIT 1
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    structured = _loads(row["structured_json"], {}) if row else {}
    card = structured.get("pending_plan_confirmation") if isinstance(structured, dict) else {}
    return dict(card) if isinstance(card, dict) and card.get("kind") and card.get("objective") else {}


def _copilot_plan_from_anchor(
    self,
    anchor: dict[str, Any],
    conversation_state: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate the plan hash/version before using an assistant confirmation."""
    workflow_id = str((anchor or {}).get("workflow_id") or "").strip()
    if not workflow_id:
        return {}, {}
    state_context = conversation_state if isinstance(conversation_state, dict) else {}
    pending_state_plan = (
        state_context.get("pending_plan")
        if isinstance(state_context.get("pending_plan"), dict)
        else {}
    )
    if str(pending_state_plan.get("workflow_id") or "") == workflow_id:
        # 展示后写入的新事实已使计划过期，或状态版本已推进：短确认不再有效。
        if pending_state_plan.get("stale_reason"):
            return {}, {}
        anchor_revision = (anchor or {}).get("state_revision")
        if anchor_revision is not None and int(state_context.get("revision") or 0) != int(anchor_revision or 0):
            return {}, {}
    try:
        state = self.get_workflow(workflow_id)
    except (ValueError, sqlite3.Error):
        return {}, {}
    workflow = state.get("workflow") if isinstance(state.get("workflow"), dict) else {}
    if str(workflow.get("status") or "") != "planned":
        return {}, {}
    actual = dict(state.get("plan_ref") or {})
    if not actual.get("workflow_id") or not actual.get("plan_hash"):
        return {}, {}
    for key in ("version", "plan_hash"):
        expected = anchor.get(key)
        if expected is not None and str(expected) != str(actual.get(key)):
            return {}, {}
    goal = state.get("goal") if isinstance(state.get("goal"), dict) else {}
    goal_context = goal.get("context") if isinstance(goal.get("context"), dict) else {}
    actual["action"] = str(actual.get("action") or goal.get("action") or workflow.get("action") or "")
    actual["target"] = dict(actual.get("target") or goal_context)
    return actual, state


def _copilot_plan_matches_selected(
    selected: dict[str, Any],
    selected_facts: dict[str, Any],
    plan_state: dict[str, Any],
    plan_ref: dict[str, Any],
) -> bool:
    """Ensure a short confirmation cannot cross a job/candidate boundary."""
    if not plan_state or not plan_ref.get("workflow_id"):
        return False
    if str(selected.get("type") or "") == "workflow":
        return str(selected.get("id") or "") == str(plan_ref.get("workflow_id") or "")
    target = plan_ref.get("target") if isinstance(plan_ref.get("target"), dict) else {}
    target_type = str(target.get("type") or "")
    target_id = str(target.get("id") or "")
    if target_type == "job":
        selected_job_id = _copilot_context_job_id(selected, selected_facts)
        return selected_job_id is not None and str(selected_job_id) == target_id
    if target_type == "candidate":
        selected_candidate = (
            selected.get("id")
            if str(selected.get("type") or "") == "candidate"
            else (selected_facts.get("candidate") or {}).get("id")
        )
        return bool(selected_candidate) and str(selected_candidate) == target_id
    return (
        str(selected.get("type") or "") == target_type
        and str(selected.get("id") or "") == target_id
    )


def _copilot_pending_plan(
    self,
    selected: dict[str, Any],
    existing_focus: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates: list[str] = []
    if selected.get("type") == "workflow" and selected.get("id"):
        candidates.append(str(selected["id"]))
    focus_context = existing_focus.get("context") if isinstance(existing_focus, dict) else {}
    if isinstance(focus_context, dict) and focus_context.get("type") == "workflow" and focus_context.get("id"):
        candidates.append(str(focus_context["id"]))
    pending = existing_focus.get("pending_workflow") if isinstance(existing_focus, dict) else {}
    if isinstance(pending, dict) and pending.get("workflow_id"):
        candidates.append(str(pending["workflow_id"]))
    for workflow_id in dict.fromkeys(candidates):
        try:
            state = self.get_workflow(workflow_id)
        except ValueError:
            continue
        status = str((state.get("workflow") or {}).get("status") or "")
        if status in TERMINAL_WORKFLOW_STATUSES:
            continue
        plan_ref = dict(state.get("plan_ref") or {})
        if plan_ref.get("workflow_id") and plan_ref.get("plan_hash"):
            return plan_ref, state
    return {}, {}


def _copilot_focus_context_facts(self, context: dict[str, Any]) -> dict[str, Any]:
    context_type = str(context.get("type") or "global")
    try:
        context_id = int(context.get("id") or 0)
    except (TypeError, ValueError):
        context_id = 0
    if context_type not in {"job", "candidate"} or context_id <= 0:
        return {}
    conn = self._connect()
    try:
        if context_type == "job":
            row = conn.execute(
                """
                SELECT j.id,c.name AS client,j.title AS job,j.status
                FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?
                """,
                (context_id,),
            ).fetchone()
            if row:
                return {
                    "context": {"type": "job", "id": context_id},
                    "client": str(row["client"] or ""),
                    "job": {"id": context_id, "title": str(row["job"] or ""), "status": str(row["status"] or "")},
                    "candidate": {},
                }
            return {}
        row = conn.execute(
            """
            SELECT jc.id,p.display_name,c.name AS client,j.id AS job_id,j.title AS job
            FROM job_candidates jc JOIN people p ON p.id=jc.person_id
            LEFT JOIN jobs j ON j.id=jc.job_id LEFT JOIN clients c ON c.id=j.client_id
            WHERE jc.id=?
            """,
            (context_id,),
        ).fetchone()
        if row:
            return {
                "context": {"type": "candidate", "id": context_id},
                "client": str(row["client"] or ""),
                "job": {"id": int(row["job_id"] or 0), "title": str(row["job"] or "")},
                "candidate": {"id": context_id, "name": str(row["display_name"] or "")},
            }
    finally:
        conn.close()
    return {}


def _copilot_workflow_context_facts(self, context: dict[str, Any]) -> dict[str, Any]:
    workflow_id = str(context.get("id") or "").strip()
    if not re.fullmatch(r"workflow_[0-9a-zA-Z]+", workflow_id):
        return {}
    conn = self._connect()
    try:
        row = conn.execute(
            """
            SELECT w.workflow_id,w.status,g.title,g.objective,g.context_type,g.context_id,g.context_json
            FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
            WHERE w.workflow_id=?
            """,
            (workflow_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {}
    workflow_context = _loads(row["context_json"], {}) or {}
    job_facts: dict[str, Any] = {}
    job_id = int(row["context_id"] or 0) if str(row["context_type"] or "") == "job" else 0
    if job_id:
        try:
            job_facts = self._copilot_focus_context_facts({"type": "job", "id": job_id})
        except sqlite3.Error:
            # Strategy patch tests intentionally use a minimal workflow-only schema.
            job_facts = {}
    return {
        "context": {"type": "workflow", "id": workflow_id},
        "workflow": {
            "workflow_id": workflow_id,
            "title": str(row["title"] or workflow_id),
            "objective": str(row["objective"] or ""),
            "status": str(row["status"] or ""),
            "context": workflow_context,
        },
        "client": str(job_facts.get("client") or ""),
        "job": dict(job_facts.get("job") or {}),
        "candidate": {},
    }


def _workflow_strategy_question(message: str, context: dict[str, Any]) -> bool:
    """Return whether the user is asking to read the selected workflow strategy."""
    if str(context.get("type") or "") != "workflow" or not context.get("id"):
        return False
    normalized = "".join(str(message or "").lower().split())
    strategy_terms = ("寻访策略", "搜索策略", "搜人策略")
    if not any(term in normalized for term in strategy_terms):
        return False
    mutation_terms = (
        "修改", "调整", "优化", "新增", "增加", "补充", "删除", "去掉", "替换", "改成", "应用",
    )
    return not any(term in normalized for term in mutation_terms)


def _compact_workflow_context(workflow_payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the current workflow facts needed by Copilot without sending full artifacts."""
    workflow = dict(workflow_payload.get("workflow") or {})
    goal = dict(workflow_payload.get("goal") or {})
    steps = [
        {
            "id": step.get("id"),
            "capability_id": step.get("capability_id"),
            "label": step.get("business_label"),
            "status": step.get("status"),
            "risk_level": step.get("risk_level"),
        }
        for step in (workflow_payload.get("steps") or [])
    ]
    approvals = [
        {
            "approval_id": approval.get("approval_id"),
            "action_type": approval.get("action_type"),
            "status": approval.get("status"),
            "risk_level": approval.get("risk_level"),
            "preflight": approval.get("preflight") or {},
        }
        for approval in (workflow_payload.get("approvals") or [])
    ]
    artifacts = workflow_payload.get("artifacts") or []
    strategy_artifact = next(
        (
            artifact for artifact in artifacts
            if artifact.get("artifact_type") == "search_strategy"
            and artifact.get("validation_status") == "passed"
        ),
        None,
    )
    strategy: dict[str, Any] | None = None
    if strategy_artifact:
        metadata = strategy_artifact.get("metadata") if isinstance(strategy_artifact.get("metadata"), dict) else {}
        plan = metadata.get("plan") if isinstance(metadata.get("plan"), dict) else {}
        strategy_v2_payload = metadata.get("strategy_v2") if isinstance(metadata.get("strategy_v2"), dict) else {}
        review_gates = plan.get("review_gates") if isinstance(plan.get("review_gates"), dict) else {}
        channels = plan.get("channels") if isinstance(plan.get("channels"), dict) else {}
        strategy = {
            "artifact_id": strategy_artifact.get("artifact_id"),
            "validation_status": strategy_artifact.get("validation_status"),
            "model": ((plan.get("generation") or {}).get("model") if isinstance(plan.get("generation"), dict) else ""),
            "summary": str(plan.get("strategy_summary") or ""),
            "channels": {
                channel: [
                    {
                        "query": str(item.get("query") or ""),
                        "purpose": str(item.get("purpose") or ""),
                    }
                    for item in items if isinstance(item, dict) and item.get("query")
                ]
                for channel, items in channels.items() if isinstance(items, list)
            },
            "target_companies": list(plan.get("target_companies") or []),
            "hard_requirements": list(review_gates.get("hard_requirements") or []),
            "negative_rules": list(review_gates.get("negative_rules") or []),
            "risk_points": list(review_gates.get("risk_points") or []),
            "input_level": strategy_v2_payload.get("input_level"),
            "missing_anchors": list(strategy_v2_payload.get("missing_anchors") or []),
            "keyword_groups": list(strategy_v2_payload.get("step4_keyword_groups") or []),
        }
    return {
        "goal": {
            "goal_id": goal.get("goal_id"),
            "title": goal.get("title"),
            "objective": goal.get("objective"),
        },
        "workflow": {
            "workflow_id": workflow.get("workflow_id"),
            "status": workflow.get("status"),
            "current_stage": workflow.get("current_stage"),
        },
        "plan_ref": dict(workflow_payload.get("plan_ref") or {}),
        "progress": dict(workflow_payload.get("progress") or {}),
        "steps": steps,
        "approvals": approvals,
        "strategy": strategy,
    }
