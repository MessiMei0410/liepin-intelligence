#!/usr/bin/env python3
"""Shared Liepin reply classification rules."""

from __future__ import annotations

import re
from typing import Any


CLASSIFIER_VERSION = "reply-rules-v5"


def normalize_message(message: str) -> str:
    text = " ".join(str(message or "").replace("\u00a0", " ").split())
    for prefix in ("[已读]", "[未读]", "[手机]"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text


def has_any(text: str, words: list[str]) -> bool:
    return any(word in text for word in words)


def add_tag(tags: list[str], tag: str) -> None:
    if tag not in tags:
        tags.append(tag)


def classify_reply(message: str) -> dict[str, Any]:
    """Classify one candidate reply into intent, blocker and next action.

    The returned keys intentionally stay compatible with liepin_im_reply_ingest.py.
    """

    text = normalize_message(message)
    lower = text.lower()
    blockers: list[str] = []
    tags: list[str] = []
    intent = "unclear"
    sentiment = "neutral"
    action = "人工复核这条回复。"
    priority = 2
    task_type = "review_reply"
    reason = "没有命中明确意图，保守进入人工复核。"
    short_confirmations = {"可以的", "好的", "嗯好", "ok", "OK", "收到", "行", "好呀"}

    if not text:
        return {
            "intent": "empty",
            "sentiment": "neutral",
            "blockers": [],
            "reply_tags": [],
            "classification_reason": "空消息。",
            "suggested_next_action": "无需处理。",
            "task_type": "none",
            "priority": 3,
            "classifier_version": CLASSIFIER_VERSION,
        }

    if has_any(text, ["不是我的专业", "不对口", "领域不对", "方向不对", "专业不符", "岗位不符", "不太合适", "不是做技术的"]):
        intent = "not_interested"
        sentiment = "negative"
        blockers.append("mismatch")
        add_tag(tags, "方向不匹配")
        action = "记录不匹配原因；如候选人价值高，再换更贴合岗位沟通。"
        priority = 3
        task_type = "record_rejection"
        reason = "候选人明确表达方向或专业不匹配。"
    elif has_any(text, ["暂不考虑", "不考虑", "没兴趣", "不感兴趣", "目前不看", "没有换工作的想法", "不找工作"]):
        intent = "not_interested"
        sentiment = "negative"
        blockers.append("no_intent")
        add_tag(tags, "暂不看机会")
        action = "尊重反馈，记录暂不考虑；如有明确时间窗口再设置后续提醒。"
        priority = 3
        task_type = "record_rejection"
        reason = "候选人明确拒绝或暂不看机会。"
    elif has_any(text, ["微信", "加个微信", "加微信", "手机号", "电话", "方便联系", "交换联系方式", "发我联系方式"]):
        intent = "need_contact"
        sentiment = "positive"
        add_tag(tags, "可转联系方式")
        action = "优先承接联系方式，同时确认岗位方向、地点、薪资和看机会意愿。"
        priority = 1
        task_type = "exchange_contact"
        reason = "候选人愿意转入联系方式或电话沟通。"
    elif has_any(text, ["薪资", "待遇", "多少钱", "年包", "月薪", "薪酬", "预算", "base", "奖金", "可谈"]):
        intent = "salary_concern"
        sentiment = "neutral"
        blockers.append("salary")
        add_tag(tags, "薪资关注")
        action = "确认当前总包、固定/奖金结构和期望区间，再判断岗位预算匹配。"
        priority = 1
        task_type = "salary_followup"
        reason = "候选人关注薪资或预算。"
    elif has_any(
        text,
        [
            "地点",
            "城市",
            "在哪",
            "哪里",
            "通勤",
            "太远",
            "搬家",
            "异地",
            "工作地点",
            "不考虑上海",
            "不考虑苏州",
            "不考虑深圳",
            "不考虑北京",
            "不考虑杭州",
            "上海以外",
            "苏州以外",
            "深圳以外",
            "北京以外",
            "杭州以外",
            "西安以外",
            "其他地方不太考虑",
            "外地",
            "离得太远",
        ]
    ) or (
        has_any(text, ["上海", "苏州", "深圳", "北京", "杭州", "西安"])
        and has_any(text, ["不考虑", "不太考虑", "以外", "太远", "只能", "接受", "在本地"])
    ):
        intent = "location_concern"
        sentiment = "neutral"
        blockers.append("location")
        add_tag(tags, "地点关注")
        action = "确认工作地点接受度、通勤/搬迁约束和家庭因素。"
        priority = 2
        task_type = "location_followup"
        reason = "候选人关注地点、城市或通勤。"
    elif has_any(text, ["哪家", "公司是哪", "什么公司", "公司名字", "哪家公司", "客户是谁", "什么岗位", "职位要求", "岗位要求", "工作年限", "几年经验", "jd", "JD", "发一下", "介绍一下", "还在招", "还有在招", "目前还有", "我们有沟通过吗", "这是哪家"]):
        intent = "need_more_info"
        sentiment = "neutral"
        add_tag(tags, "要岗位信息")
        if has_any(text, ["还在招", "还有在招", "目前还有"]):
            add_tag(tags, "确认是否在招")
        action = "补充可透露的公司/岗位/JD核心信息，再引导电话确认基本匹配。"
        priority = 1
        task_type = "send_job_info"
        reason = "候选人要求了解公司、岗位或 JD。"
    elif has_any(text, ["这是我的简历", "我的经验和技能与之很匹配", "与之很匹配", "加入贵公司的机会", "期待进一步沟通", "期盼回复", "想应聘", "渴望能够得到一个加入", "合适的话可以随时联系我", "匹配度很高", "您看下合适吗", "看下合适吗"]):
        intent = "self_recommendation"
        sentiment = "positive"
        add_tag(tags, "主动投递")
        action = "先快速判断匹配度，补岗位锚点后再约沟通，不要直接进入强筛选。"
        priority = 2
        task_type = "self_recommendation_followup"
        reason = "候选人是在主动投递或主动递简历，热度不错，但仍需先补项目锚点。"
    elif has_any(text, ["我对您在招的", "职位很感兴趣", "岗位很感兴趣", "希望可以详聊", "进一步沟通", "能聊聊吗", "可以聊聊呀"]):
        intent = "targeted_interest"
        sentiment = "positive"
        add_tag(tags, "定向岗位兴趣")
        action = "承接兴趣并锁定岗位锚点，再问一个关键条件，避免直接泛泛约电话。"
        priority = 1
        task_type = "targeted_interest_followup"
        reason = "候选人对某个具体岗位表达了定向兴趣。"
    elif text.strip() in short_confirmations:
        intent = "short_confirmation"
        sentiment = "positive"
        add_tag(tags, "短确认")
        add_tag(tags, "轻跟进")
        action = "先补岗位锚点或核心信息，再轻量追问一个问题，不急着直接约电话。"
        priority = 3
        task_type = "light_touch_followup"
        reason = "候选人只有简短确认，说明没有拒绝，但热度和信息量都偏弱。"
    elif has_any(text, ["可以聊", "聊聊", "能聊", "有兴趣", "感兴趣", "匹配", "合适", "详聊", "进一步沟通", "可以看看", "可以了解", "发来看看", "应聘", "期盼回复", "期待回复", "希望详聊", "您看下合适吗", "看下合适吗", "加入贵公司"]):
        intent = "interested"
        sentiment = "positive"
        add_tag(tags, "正向意向")
        if has_any(text, ["您看下合适吗", "看下合适吗"]):
            add_tag(tags, "自荐匹配")
        if has_any(text, ["应聘", "期盼回复", "期待回复", "加入贵公司"]):
            add_tag(tags, "主动应聘")
        action = "当天跟进：补岗位信息，并约 10 分钟电话确认动机、薪资、地点。"
        priority = 1
        task_type = "call_candidate"
        reason = "候选人表达可聊、感兴趣或愿意进一步了解。"
    elif has_any(text, ["已离职", "刚离职", "在看机会", "正在看", "考虑机会", "最近在看"]):
        intent = "interested"
        sentiment = "positive"
        add_tag(tags, "正在看机会")
        action = "优先跟进，确认近期到岗时间、核心诉求和薪资区间。"
        priority = 1
        task_type = "call_candidate"
        reason = "候选人透露正在看机会或可快速推进。"
    elif has_any(text, ["负责三次电源", "负责什么方向", "主要方向", "产品方向", "他们主要搞", "做什么的", "哪一块业务", "vr供电", "做哪一块"]):
        intent = "need_more_info"
        sentiment = "neutral"
        add_tag(tags, "方向确认")
        action = "先回答岗位/客户方向，再只问一个是否继续看机会或是否方便沟通。"
        priority = 1
        task_type = "send_job_info"
        reason = "候选人询问岗位、客户或业务方向。"
    elif has_any(text, ["晚点", "过段时间", "年后", "下个月", "之后再说", "后面再看", "现在忙"]):
        intent = "unclear"
        sentiment = "neutral"
        add_tag(tags, "需延后跟进")
        action = "回复承接并确认方便跟进的时间点，设置后续提醒。"
        priority = 2
        task_type = "review_reply"
        reason = "候选人没有拒绝，但需要延后沟通。"

    if intent != "salary_concern" and has_any(text, ["薪资", "待遇", "多少钱", "年包", "月薪", "薪酬", "预算", "base", "奖金", "可谈"]):
        blockers.append("salary")
        add_tag(tags, "薪资关注")
    if intent != "location_concern" and has_any(text, ["地点", "城市", "在哪", "哪里", "通勤", "太远", "搬家", "异地"]):
        blockers.append("location")
        add_tag(tags, "地点关注")
    if intent != "need_more_info" and has_any(text, ["哪家", "公司是哪", "什么公司", "公司名字", "哪家公司", "客户是谁", "什么岗位", "职位要求", "岗位要求", "工作年限", "几年经验", "还在招", "还有在招", "目前还有", "jd", "JD"]):
        add_tag(tags, "要岗位信息")
    if has_any(text, ["还在招", "还有在招", "目前还有"]):
        add_tag(tags, "确认是否在招")
    if has_any(text, ["苏州机会", "上海以外", "区域", "地点", "城市", "工作地点"]):
        add_tag(tags, "地点关注")
    if "fpag" in lower or "fpga" in lower:
        blockers.append("domain_fpga")
        add_tag(tags, "FPGA线索")
    if re.search(r"\b(ic|eda|pvd|cvd|mes|amhs|cim)\b", lower):
        add_tag(tags, "半导体关键词")

    return {
        "intent": intent,
        "sentiment": sentiment,
        "blockers": sorted(set(blockers)),
        "reply_tags": tags,
        "classification_reason": reason,
        "suggested_next_action": action,
        "task_type": task_type,
        "priority": priority,
        "classifier_version": CLASSIFIER_VERSION,
    }
