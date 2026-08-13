from __future__ import annotations

import copy
import hashlib
import re
from typing import Any


STATE_VERSION = "copilot_context_state_v2"
TURN_VERSION = "copilot_turn_understanding_v2"
TERMINAL_WORKFLOW_STATUSES = {"completed", "cancelled", "superseded", "archived", "failed"}

_SPEECH_ACTS = {
    "ask", "inform", "discuss", "propose", "confirm", "execute", "correct", "cancel", "other",
}
_ACTIONS = {
    "none", "candidate_sourcing", "strategy_revision", "candidate_outreach",
    "candidate_review", "job_publish", "job_split", "job_archive",
    "recommendation", "salary",
}
_FACT_KINDS = {
    "job_budget", "job_requirement", "candidate_compensation", "candidate_availability",
    "candidate_preference", "client_preference", "workflow_observation", "other",
}
_FACT_LABELS = {
    "job_budget": "岗位预算",
    "job_requirement": "岗位要求",
    "candidate_compensation": "人选薪资",
    "candidate_availability": "人选到岗",
    "candidate_preference": "人选意向",
    "client_preference": "客户偏好",
    "workflow_observation": "执行观察",
    "other": "已确认事实",
}

_ACTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "candidate_sourcing": (
        r"(?:给|为).{0,40}(?:找|寻访|搜索|搜寻|补充|补池).{0,30}(?:人选|候选人)",
        r"(?:找|寻访|搜索|搜寻|补池|补充).{0,24}(?:人选|候选人)",
        r"(?:人选|候选人).{0,24}(?:再找|继续找|补充|补池|寻访|搜索)",
        r"(?:跑|开|启动|开始|继续|重新|再跑).{0,12}(?:一轮|新一轮)?.{0,12}(?:寻访|搜索)",
        r"(?:这个|当前|该).{0,12}(?:岗位|职位).{0,12}(?:再来|再跑|继续).{0,8}(?:一轮|新一轮)",
        r"(?:再触达|补充触达|补充并触达|再联系).{0,24}(?:一些|一批|新)?.{0,8}(?:人选|候选人)",
    ),
    "strategy_revision": (
        r"(?:修改|调整|修订|更新|优化|重做).{0,30}(?:寻访策略|搜索策略|计划|公司池|关键词|排除规则)",
        r"(?:寻访策略|搜索策略|计划|公司池|关键词|排除规则).{0,30}(?:修改|调整|修订|更新|优化|重做)",
    ),
    "candidate_outreach": (
        r"(?:帮我|请|现在|立即|开始|继续|批量)?.{0,12}(?:联系|触达|开聊|沟通|发消息).{0,30}(?:人选|候选人|这批|他们|他|她)",
        r"(?:人选|候选人|这批).{0,24}(?:联系|触达|开聊|沟通|发消息)",
    ),
    "candidate_review": (
        r"(?:帮我|请|开始|重新|批量)?.{0,12}(?:评估|复核|分析|筛选|判断).{0,30}(?:人选|候选人|简历|匹配度)",
        r"(?:人选|候选人|简历).{0,24}(?:评估|复核|分析|筛选|判断)",
        r"(?:过滤|筛选|分级|分层|按证据|按硬性|按匹配度).{0,40}(?:候选池|候选人|人选|名单|列表|输出)",
        r"(?:候选池|候选人|人选|名单|列表).{0,40}(?:过滤|筛选|分级|分层|按证据|按硬性|按匹配度)",
        r"(?:比较|对比|排序).{0,30}(?:前|top|TOP)?\s*\d*\s*(?:位|个|名)?(?:候选池|候选人|人选)",
        r"(?:比较|对比|排序).{0,20}(?:前|top|TOP)?\s*\d+\s*(?:位|个|名|人)",
        r"(?:候选池|候选人|人选).{0,30}(?:比较|对比|排序).{0,20}(?:前|top|TOP)?\s*\d*",
    ),
    "job_publish": (
        r"(?:发布|上架|发到猎聘|准备发布).{0,24}(?:岗位|职位)",
        r"(?:岗位|职位).{0,24}(?:发布|上架|发到猎聘)",
    ),
    "job_split": (
        r"(?:拆分|拆成|分成|新建|建立|录入).{0,24}(?:岗位|职位)",
        r"(?:岗位|职位).{0,24}(?:拆分|拆成|分成|新建|建立|录入)",
    ),
    "job_archive": (
        r"(?:归档|关闭|下线).{0,24}(?:岗位|职位)",
        r"(?:岗位|职位).{0,24}(?:归档|关闭|下线)",
    ),
    "recommendation": (
        r"(?:帮我|请|生成|整理|制作|提交|开始)?.{0,12}(?:推荐报告|推荐材料|推荐给客户|推给客户|提交客户)",
        r"(?:把|将).{0,20}(?:人选|候选人|他|她).{0,20}(?:推荐给客户|推给客户|提交客户)",
    ),
    "salary": (
        r"(?:帮我|请|立即|现在|开始|执行|启动|生成|整理|制作|处理|做一份).{0,20}(?:谈薪|薪资谈判|谈薪方案|薪资核验|薪资报告|谈薪风险|薪资材料)",
        r"(?:谈薪方案|薪资核验|薪资报告|谈薪风险|薪资材料).{0,16}(?:生成|整理|制作|任务|计划|报告)",
    ),
}


def _clean(value: Any, limit: int = 500) -> str:
    return " ".join(str(value or "").split())[:limit]


def _stable_id(*parts: Any) -> str:
    raw = "|".join(_clean(part, 500) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _looks_like_question(message: str) -> bool:
    text = _clean(message)
    return bool(
        re.search(r"[?？]$", text)
        or re.search(r"(?:吗|呢|如何|怎么|为什么|为何|是否|能不能|可不可以|要不要|什么|多少|哪(?:个|些)?)", text)
    )


_FACT_RETRACT_PATTERNS = (
    r"(?:不要|别|不用|不可以)记(?:录|下)?(?:了|吧)?",
    r"(?:那条|这条|刚才那条|刚刚那条|上一条)(?:记录|事实)?(?:不算|作废|撤掉|撤回|去掉)",
    r"(?:把)?(?:刚才|刚刚|上一条)?(?:那条|这条)?(?:记录|事实)(?:不算|作废|撤掉|撤回|删掉|去掉)",
)

_UNDO_TASK_PATTERNS = (
    r"(?:撤销|撤回|撤掉|作废|删除|删掉)[^，。；;]{0,12}(?:任务|工作流)",
    r"(?:刚才|刚刚|刚)[^，。；;]{0,12}(?:任务|工作流)[^，。；;]{0,8}(?:不要了?|撤销|撤回|撤掉|作废|取消)",
    r"(?:任务|工作流)[^，。；;]{0,6}(?:不要了)",
)


def undo_task_requested(message: str) -> bool:
    """“撤销刚才创建的任务/刚才那个任务不要了”：针对本会话刚建的工作流。

    与 cancel plan 的“取消”区分：cancel 针对待确认计划本身（“取消计划”），
    这里只匹配明确指向“任务/工作流”的撤销措辞。
    """
    text = _clean(message)
    if not text or _looks_like_question(text):
        return False
    return any(re.search(pattern, text) for pattern in _UNDO_TASK_PATTERNS)


def fact_retract_requested(message: str) -> bool:
    """“刚才那条不要记/别记/那条不算”：撤销最近一条已记录事实（非物理删除）。"""
    text = _clean(message)
    if not text or _looks_like_question(text):
        return False
    if undo_task_requested(text):
        return False
    return any(re.search(pattern, text) for pattern in _FACT_RETRACT_PATTERNS)


def fact_scope_correction_request(message: str) -> dict[str, str] | None:
    """“不是这个岗位/不是这个人，是XXX”：对象级纠错，迁移最近事实的 scope。

    返回 {"previous_type", "mode", "name"}；mode 为 job/candidate/named。
    解析不出目标形态时返回 None，交给既有 correct/澄清路径。
    """
    text = _clean(message)
    if not text or _looks_like_question(text):
        return None
    match = re.search(r"不是这(?:个|一|位)?(?P<from>岗位|职位|人选|候选人|人)", text)
    if not match:
        return None
    previous_type = "job" if match.group("from") in {"岗位", "职位"} else "candidate"
    target = re.search(r"是给(?P<to>岗位|职位|候选人|人选|这个人|这人)(?:记|录|用)?的", text) or re.search(
        r"记到(?P<to>岗位|职位|候选人|人选)", text
    )
    if target:
        return {
            "previous_type": previous_type,
            "mode": "job" if target.group("to") in {"岗位", "职位"} else "candidate",
            "name": "",
        }
    named = re.search(r"是(?P<name>[^，。；;]{1,30}?)的?(?:那个|这个|这家)(?:岗位|职位|人选|候选人)?", text)
    if named:
        return {"previous_type": previous_type, "mode": "named", "name": _clean(named.group("name"), 60)}
    return None


def latest_correctable_fact(facts: Any, scope_type: str = "") -> dict[str, Any] | None:
    """最近一条未撤销的事实；给定 scope_type 时优先同类型 scope 的最近一条。"""
    items = (
        [item for item in facts if isinstance(item, dict) and not item.get("retracted")]
        if isinstance(facts, list)
        else []
    )
    if scope_type:
        for item in reversed(items):
            if str((item.get("scope") or {}).get("type") or "") == scope_type:
                return item
    return items[-1] if items else None


def _looks_like_observation(message: str) -> bool:
    text = _clean(message)
    return bool(
        re.search(r"(?:这轮|本轮|目前|现在|已经|已|只|才).{0,30}(?:找到|召回|入库|评估|完成|失败|人选|候选人)", text)
        or re.search(r"(?:这个|该|当前).{0,16}(?:人选|候选人).{0,20}(?:匹配|合适|不合适|完美)", text)
    )


def _detect_topic(message: str, action: str, raw_topic: Any) -> str:
    topic = _clean(raw_topic, 48).lower()
    if topic:
        return topic
    if action != "none":
        return action
    text = _clean(message).lower()
    for candidate, markers in (
        ("salary", ("薪资", "薪酬", "预算", "总包", "谈薪")),
        ("sourcing", ("寻访", "搜索", "补池", "候选人", "人选")),
        ("candidate_match", ("匹配", "简历", "复核", "评估")),
        ("job", ("岗位", "职位", "jd", "要求")),
        ("workflow", ("工作流", "任务", "计划", "进展", "结果")),
    ):
        if any(marker in text for marker in markers):
            return candidate
    return "general"


def _budget_value(message: str) -> str:
    # Accept the range forms consultants commonly use: 80-120w, 80w-120w,
    # and 80至120万. The unit may appear on either side, but a range must
    # contain at least one unit so plain candidate counts are not budgets.
    match = re.search(
        r"(?:\d+(?:\.\d+)?\s*(?:w|W|万|k|K)?\s*[-~至到]\s*\d+(?:\.\d+)?\s*(?:w|W|万|k|K)"
        r"|\d+(?:\.\d+)?\s*(?:w|W|万|k|K)\s*[-~至到]\s*\d+(?:\.\d+)?\s*(?:w|W|万|k|K)?)"
        r"|\d+(?:\.\d+)?\s*(?:w|W|万|k|K)",
        message,
    )
    return _clean(match.group(0), 80) if match else ""


def _deterministic_fact_updates(message: str) -> list[dict[str, str]]:
    text = _clean(message)
    if not text or _looks_like_question(text):
        return []
    rows: list[dict[str, str]] = []
    budget_value = _budget_value(text)
    candidate_subject = bool(re.search(r"(?:这个|该|当前|这位)?(?:人选|候选人|候选|人儿)", text))
    candidate_budget = bool(
        candidate_subject
        and budget_value
        and any(marker in text for marker in ("预算", "薪资", "薪酬", "总包", "期望", "预期", "目前", "现在", "当前"))
    )
    job_budget = bool(
        budget_value
        and not candidate_budget
        and any(marker in text for marker in ("预算", "薪资范围", "薪酬范围", "年薪范围", "总包范围", "总包上限"))
    )
    if job_budget:
        rows.append({"kind": "job_budget", "quote": text, "value": budget_value})
    if (
        any(marker in text for marker in ("岗位", "职位", "这个岗", "该岗位"))
        and any(marker in text for marker in ("要求", "必须", "优先", "最好", "可看", "不要", "排除", "年限", "经验", "学历", "地点", "汇报"))
    ):
        rows.append({"kind": "job_requirement", "quote": text, "value": text})
    if any(marker in text for marker in ("候选人", "人选", "他", "她")):
        compensation_fact = any(
            marker in text
            for marker in ("当前薪资", "期望薪资", "目前薪资", "现在薪资", "总包", "涨幅")
        ) or bool(
            budget_value
            and (
                any(marker in text for marker in ("目前", "现在", "当前", "期望", "预期"))
                or candidate_budget
            )
        )
        if compensation_fact:
            # 保留整句，避免“目前 80w，期望 100w”只留下第一个金额。
            rows.append({"kind": "candidate_compensation", "quote": text, "value": text})
        if any(marker in text for marker in ("到岗", "离职", "notice", "入职时间")):
            rows.append({"kind": "candidate_availability", "quote": text, "value": text})
        if any(marker in text for marker in ("意向", "倾向", "不考虑", "愿意", "接受")):
            rows.append({"kind": "candidate_preference", "quote": text, "value": text})
    if "客户" in text and any(marker in text for marker in ("偏好", "要求", "反馈", "不要", "优先")):
        rows.append({"kind": "client_preference", "quote": text, "value": text})
    if _looks_like_observation(text):
        rows.append({"kind": "workflow_observation", "quote": text, "value": text})
    return rows


def _validated_fact_updates(message: str, values: Any) -> list[dict[str, str]]:
    text = _clean(message)
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, dict):
            continue
        quote = _clean(value.get("quote"))
        if not quote or quote not in text:
            continue
        kind = _clean(value.get("kind"), 48).lower()
        if kind not in _FACT_KINDS:
            kind = "other"
        item = {"kind": kind, "quote": quote, "value": _clean(value.get("value") or quote)}
        key = (item["kind"], item["quote"])
        if key not in seen:
            seen.add(key)
            rows.append(item)
    for item in _deterministic_fact_updates(text):
        key = (item["kind"], item["quote"])
        if key not in seen:
            seen.add(key)
            rows.append(item)
    return rows[-8:]


def action_evidence_for_turn(
    understanding: dict[str, Any],
    *,
    message: str,
    pending_plan_ref: dict[str, Any] | None = None,
) -> list[str]:
    text = _clean(message)
    action = _clean(understanding.get("action"), 48).lower()
    speech_act = _clean(understanding.get("speech_act"), 24).lower()
    pending = dict(pending_plan_ref or {})
    if not text or _looks_like_question(text):
        return []
    if _looks_like_observation(text) and not re.search(r"(?:帮我|请|给我|继续|再找|重新|开始|执行|启动)", text):
        return []
    if speech_act == "confirm":
        return [text] if pending.get("workflow_id") and re.fullmatch(
            r"(?:好|好的|可以|行|确认|按这个来|就这样|开始|开始吧|执行|继续|可以搜索|可以寻访|开始搜索|开始寻访|继续搜索|继续寻访)",
            re.sub(r"[\s。.!！?？,，、]+", "", text),
            re.I,
        ) else []
    if speech_act == "cancel":
        return [text] if pending.get("workflow_id") and re.search(r"(?:取消|算了|停止这个计划|不要执行|终止)", text) else []
    if speech_act == "correct" and pending.get("workflow_id"):
        has_change = bool(understanding.get("constraint_changes") or understanding.get("raw_constraint_changes"))
        has_constraint = bool(understanding.get("constraints"))
        correction_marker = bool(re.search(r"(?:纠正|更正|改一下|改下|改成|改为|调整为|放宽|收紧|去掉|删除|移除|不再要求|不用卡)", text))
        if correction_marker and (has_change or has_constraint):
            return [text]
    if action not in _ACTIONS or action == "none":
        return []
    return [text] if any(re.search(pattern, text, re.I) for pattern in _ACTION_PATTERNS.get(action, ())) else []


def enrich_turn_understanding(
    understanding: dict[str, Any],
    *,
    message: str,
    pending_plan_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = dict(understanding or {})
    speech_act = _clean(result.get("speech_act"), 24).lower()
    if speech_act not in _SPEECH_ACTS:
        speech_act = "other"
    action = _clean(result.get("action"), 48).lower()
    if action not in _ACTIONS:
        action = "none"
    topic = _detect_topic(message, action, result.get("topic"))
    fact_updates = _validated_fact_updates(message, result.get("fact_updates"))
    result.update({"speech_act": speech_act, "action": action, "topic": topic, "fact_updates": fact_updates})
    evidence = action_evidence_for_turn(result, message=message, pending_plan_ref=pending_plan_ref)

    if speech_act in {"ask", "inform", "discuss", "other"}:
        # 模型有时会把“按证据分级过滤候选池”标成 other。只有确定性
        # 动作证据存在时才提升为命令；观察句仍保持 no-action。
        if evidence and action != "none":
            speech_act = "propose"
        else:
            action = "none"
    elif not evidence and speech_act in {"propose", "execute"}:
        if fact_updates or _looks_like_observation(message):
            speech_act = "inform"
        else:
            speech_act = "other"
        action = "none"
    elif not evidence and speech_act == "other" and fact_updates:
        speech_act = "inform"
    elif not evidence and speech_act == "correct" and not (pending_plan_ref or {}).get("workflow_id"):
        action = "none"

    turn_kind = {
        "ask": "question",
        "inform": "fact_update" if fact_updates else "observation",
        "discuss": "discussion",
        "propose": "command",
        "execute": "command",
        "confirm": "confirmation",
        "correct": "correction",
        "cancel": "cancellation",
    }.get(speech_act, "other")
    result.update({
        "version": TURN_VERSION,
        "speech_act": speech_act,
        "action": action,
        "topic": topic,
        "turn_kind": turn_kind,
        "action_evidence": evidence,
        "safe_for_action": bool(
            action != "none"
            and speech_act in {"propose", "confirm", "execute", "correct", "cancel"}
            and evidence
            and float(result.get("confidence") or 0.0) >= 0.72
            and not result.get("needs_clarification")
        ),
    })
    return result


def _normalized_context(context: dict[str, Any], focus: dict[str, Any]) -> dict[str, Any]:
    focus_context = focus.get("context") if isinstance(focus.get("context"), dict) else {}
    selected = focus_context or context or {"type": "global", "id": None}
    job = focus.get("job") if isinstance(focus.get("job"), dict) else {}
    candidate = focus.get("candidate") if isinstance(focus.get("candidate"), dict) else {}
    return {
        "type": _clean(selected.get("type") or "global", 32),
        "id": selected.get("id"),
        "client": _clean(focus.get("client"), 120),
        "job": {"id": job.get("id"), "title": _clean(job.get("title") or job.get("job"), 180)} if job else {},
        "candidate": {"id": candidate.get("id"), "name": _clean(candidate.get("name"), 120)} if candidate else {},
        "confidence": round(float(focus.get("confidence") or 0.0), 3),
    }


def _fact_scope(kind: str, active_context: dict[str, Any]) -> dict[str, Any]:
    if kind.startswith("job_") and (active_context.get("job") or {}).get("id"):
        return {"type": "job", "id": active_context["job"]["id"]}
    if kind.startswith("candidate_") and (active_context.get("candidate") or {}).get("id"):
        return {"type": "candidate", "id": active_context["candidate"]["id"]}
    return {"type": active_context.get("type") or "global", "id": active_context.get("id")}


def build_context_state(
    previous: dict[str, Any] | None,
    *,
    message: str,
    context: dict[str, Any],
    business_focus: dict[str, Any],
    understanding: dict[str, Any],
    decision: dict[str, Any],
    workflow_intent: dict[str, Any] | None,
    now: str,
) -> dict[str, Any]:
    state = copy.deepcopy(previous) if isinstance(previous, dict) else {}
    revision = int(state.get("revision") or 0) + 1
    active_context = _normalized_context(context, business_focus)
    facts = [dict(item) for item in state.get("facts") or [] if isinstance(item, dict)]
    observations = [dict(item) for item in state.get("observations") or [] if isinstance(item, dict)]
    corrections = [dict(item) for item in state.get("corrections") or [] if isinstance(item, dict)]
    new_fact_kinds: list[str] = []

    for update in understanding.get("fact_updates") or []:
        if not isinstance(update, dict):
            continue
        kind = _clean(update.get("kind"), 48) or "other"
        quote = _clean(update.get("quote"))
        if not quote:
            continue
        # 观察是对执行结果的未验证陈述，不能进入事实账本或约束推理。
        if kind == "workflow_observation":
            observations.append({
                "id": _stable_id("observation", quote, now),
                "kind": kind,
                "quote": quote,
                "value": _clean(update.get("value") or quote),
                "scope": _fact_scope(kind, active_context),
                "verified": False,
                "source": "user_observation",
                "at": now,
            })
            continue
        scope = _fact_scope(kind, active_context)
        key = _stable_id(kind, scope.get("type"), scope.get("id"))
        previous_fact = next((item for item in facts if item.get("id") == key), None)
        if previous_fact and previous_fact.get("quote") != quote:
            corrections.append({
                "id": _stable_id("fact_correction", key, quote, now),
                "kind": kind,
                "previous_quote": _clean(previous_fact.get("quote")),
                "quote": quote,
                "at": now,
            })
            facts = [item for item in facts if item.get("id") != key]
        facts.append({
            "id": key,
            "kind": kind,
            "quote": quote,
            "value": _clean(update.get("value") or quote),
            "scope": scope,
            "source": "user",
            "at": now,
        })
        new_fact_kinds.append(kind)

    if understanding.get("fact_retract"):
        # 事实撤销：不物理删除，打 retracted 标记并写 corrections 留痕。
        retract_target = latest_correctable_fact(facts)
        if retract_target is not None:
            retract_target["retracted"] = True
            corrections.append({
                "id": _stable_id("fact_retract", retract_target.get("id"), now),
                "kind": "fact_retract",
                "fact_kind": _clean(retract_target.get("kind"), 48),
                "previous_quote": _clean(retract_target.get("quote")),
                "quote": "",
                "at": now,
            })

    scope_fix = understanding.get("fact_scope_correction")
    if isinstance(scope_fix, dict) and isinstance(scope_fix.get("new_scope"), dict):
        # 对象级纠错：把最近事实的 scope 迁移到用户指定的岗位/候选人。
        new_scope = {
            "type": _clean(scope_fix["new_scope"].get("type"), 32),
            "id": scope_fix["new_scope"].get("id"),
        }
        scope_target = latest_correctable_fact(facts, _clean(scope_fix.get("previous_type"), 32))
        if scope_target is not None and new_scope.get("type"):
            previous_scope = dict(scope_target.get("scope") or {})
            new_key = _stable_id(scope_target.get("kind"), new_scope.get("type"), new_scope.get("id"))
            corrections.append({
                "id": _stable_id("fact_scope", scope_target.get("id"), new_key, now),
                "kind": "fact_scope",
                "fact_kind": _clean(scope_target.get("kind"), 48),
                "quote": _clean(scope_target.get("quote")),
                "previous_scope": previous_scope,
                "new_scope": new_scope,
                "at": now,
            })
            migrated = dict(scope_target)
            migrated["scope"] = new_scope
            migrated["id"] = new_key
            facts = [item for item in facts if item is not scope_target and item.get("id") != new_key]
            facts.append(migrated)

    for change in decision.get("constraint_changes") or []:
        if not isinstance(change, dict):
            continue
        corrections.append({
            "id": _stable_id("constraint_change", change.get("operation"), change.get("previous_quote"), change.get("quote"), now),
            "kind": "constraint",
            "operation": _clean(change.get("operation"), 24),
            "previous_quote": _clean(change.get("previous_quote")),
            "quote": _clean(change.get("quote")),
            "at": now,
        })

    active_goal = dict(state.get("active_goal") or {})
    effect = _clean(decision.get("effect"), 32)
    action = _clean(understanding.get("action"), 48)
    if effect in {"create_plan", "revise_plan", "start_plan"} and action and action != "none":
        active_goal = {
            "action": action,
            "objective": _clean(understanding.get("objective") or message),
            "target": dict(understanding.get("target") or {}),
            "status": "draft" if effect == "create_plan" else "active",
            "source_quote": _clean(message),
            "updated_at": now,
        }
    if effect == "cancel_plan":
        active_goal = {}

    pending_plan = dict(state.get("pending_plan") or {})
    plan_refreshed = bool(isinstance(workflow_intent, dict) and workflow_intent.get("workflow_id"))
    if plan_refreshed:
        workflow_status = _clean(workflow_intent.get("status"), 48)
        plan = {
            key: workflow_intent.get(key)
            for key in ("workflow_id", "status", "version", "plan_hash", "action", "objective")
        }
        plan["updated_at"] = now
        if workflow_status in TERMINAL_WORKFLOW_STATUSES:
            pending_plan = {}
        else:
            pending_plan = plan
            if active_goal:
                active_goal["status"] = workflow_status or active_goal.get("status") or "planned"
                active_goal["workflow_id"] = workflow_intent.get("workflow_id")
    elif effect == "cancel_plan":
        pending_plan = {}
    if (new_fact_kinds or decision.get("constraint_changes")) and pending_plan.get("workflow_id") and not plan_refreshed:
        # 本轮写入了新事实或纠正了约束，而待确认计划仍基于之前的信息：
        # 标记过期原因，由消费侧（计划锚点/短确认）要求重算，不能直接启动旧计划。
        stale_reasons = [item for item in str(pending_plan.get("stale_reason") or "").split(",") if item]
        stale_reasons = list(dict.fromkeys([
            *stale_reasons,
            *(f"{kind}_updated" for kind in new_fact_kinds),
            *("constraints_updated" for _ in decision.get("constraint_changes") or []),
        ]))
        pending_plan = dict(pending_plan)
        pending_plan["stale_reason"] = ",".join(stale_reasons)
        pending_plan["last_referenced_at"] = pending_plan.get("updated_at") or now
        pending_plan["state_revision"] = revision

    open_questions = [dict(item) for item in state.get("open_questions") or [] if isinstance(item, dict)]
    if effect == "clarify":
        question = _clean(understanding.get("clarification_question") or "需要确认当前对象或动作")
        open_questions.append({"id": _stable_id(question), "question": question, "at": now})
    elif effect in {"create_plan", "revise_plan", "start_plan", "cancel_plan"}:
        open_questions = []

    effective_constraints = decision.get("effective_constraints")
    constraints = (
        [dict(item) for item in effective_constraints if isinstance(item, dict)]
        if isinstance(effective_constraints, list)
        else [dict(item) for item in state.get("constraints") or [] if isinstance(item, dict)]
    )
    return {
        "version": STATE_VERSION,
        "revision": revision,
        "active_context": active_context,
        "facts": facts[-32:],
        "observations": observations[-24:],
        "corrections": corrections[-24:],
        "constraints": constraints[-24:],
        "active_goal": active_goal,
        "open_questions": open_questions[-8:],
        "pending_plan": pending_plan,
        "last_turn": {
            "kind": _clean(understanding.get("turn_kind"), 32),
            "topic": _clean(understanding.get("topic"), 48),
            "requested_action": action or "none",
            "effect": effect or "answer",
            "action_evidence": list(understanding.get("action_evidence") or []),
            "source_quote": _clean(message),
            "at": now,
        },
        "updated_at": now,
    }


def stale_reason_text(stale_reason: Any) -> str:
    """把机器可读的过期原因（如 job_budget_updated）转成中文标签。"""
    labels: list[str] = []
    for reason in str(stale_reason or "").split(","):
        reason = reason.strip()
        if not reason:
            continue
        kind = reason[:-len("_updated")] if reason.endswith("_updated") else reason
        labels.append(f"{_FACT_LABELS.get(kind, kind)}更新")
    return "、".join(labels)


def deterministic_context_summary(state: dict[str, Any] | None) -> dict[str, Any]:
    state = state if isinstance(state, dict) else {}
    context = state.get("active_context") if isinstance(state.get("active_context"), dict) else {}
    goal = state.get("active_goal") if isinstance(state.get("active_goal"), dict) else {}
    plan = state.get("pending_plan") if isinstance(state.get("pending_plan"), dict) else {}
    entities: list[dict[str, Any]] = []
    if context.get("client"):
        entities.append({"type": "client", "id": None, "name_or_title": context["client"]})
    if (context.get("job") or {}).get("id"):
        entities.append({
            "type": "job", "id": context["job"]["id"],
            "name_or_title": context["job"].get("title") or "",
        })
    if (context.get("candidate") or {}).get("id"):
        entities.append({
            "type": "candidate", "id": context["candidate"]["id"],
            "name_or_title": context["candidate"].get("name") or "",
        })
    key_facts = [
        f"{_FACT_LABELS.get(str(item.get('kind') or ''), '已确认事实')}：{_clean(item.get('quote'))}"
        for item in (state.get("facts") or [])[-12:]
        if isinstance(item, dict) and _clean(item.get("quote")) and not item.get("retracted")
    ]
    pending: list[str] = []
    if plan.get("workflow_id") and plan.get("status") == "planned":
        plan_label = f"待确认计划 {plan['workflow_id']}"
        stale_text = stale_reason_text(plan.get("stale_reason"))
        if stale_text:
            based_at = _clean(plan.get("last_referenced_at"), 32) or "此前"
            plan_label += f"（已过期：{stale_text}，计划基于 {based_at} 的信息）"
        pending.append(plan_label)
    pending.extend(
        _clean(item.get("question"))
        for item in state.get("open_questions") or []
        if isinstance(item, dict) and _clean(item.get("question"))
    )
    stage = "对话中"
    if goal.get("action"):
        stage = f"{goal['action']}:{goal.get('status') or 'active'}"
    return {
        "stage": stage,
        "entities": entities,
        "decisions": [goal.get("objective")] if goal.get("objective") else [],
        "pending": pending,
        "key_facts": key_facts,
    }
