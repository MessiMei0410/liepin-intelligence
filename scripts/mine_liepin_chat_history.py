#!/usr/bin/env python3
"""Mine historical Liepin chat snippets into reply algorithm rules."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from reply_intelligence_rules import classify_reply, normalize_message


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


CHAT_RULE_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_history_reply_rules (
    rule_key TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    trigger_text TEXT NOT NULL,
    recommended_pattern TEXT NOT NULL,
    avoid_pattern TEXT,
    evidence_count INTEGER DEFAULT 0,
    confidence INTEGER DEFAULT 3,
    examples_json TEXT DEFAULT '[]',
    updated_at TEXT DEFAULT (datetime('now','localtime'))
)
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(CHAT_RULE_SCHEMA)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_history_reply_rules_category ON chat_history_reply_rules(category)")
    conn.commit()


def load_talk_samples(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not table_exists(conn, "talk_samples"):
        return []
    return conn.execute(
        """
        SELECT id, candidate_name, candidate_title, time_text, direction_guess,
               strategy_guess, message, raw_text, collected_at
        FROM talk_samples
        ORDER BY id
        """
    ).fetchall()


def load_outreach(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not table_exists(conn, "outreach_events"):
        return []
    return conn.execute(
        """
        SELECT candidate_name, client, position, event_type, message_summary, event_time, created_at
        FROM outreach_events
        WHERE event_type IN ('reply_assistant_accept','reply_assistant_fill','chat','greeting_open_chat','already_continue_chat')
        ORDER BY datetime(COALESCE(event_time, created_at)) DESC, id DESC
        """
    ).fetchall()


def load_samples(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not table_exists(conn, "reply_assistant_samples"):
        return []
    return conn.execute(
        """
        SELECT candidate_name, candidate_title, latest_message, strategy_key,
               project_client, project_position, edited_draft, original_draft,
               changed, length_delta, accepted_at
        FROM reply_assistant_samples
        ORDER BY datetime(accepted_at) DESC
        """
    ).fetchall()


def confidence(count: int) -> int:
    if count >= 12:
        return 5
    if count >= 6:
        return 4
    if count >= 2:
        return 3
    return 2 if count else 1


def example_text(rows: list[dict[str, Any]], limit: int = 4) -> list[str]:
    examples: list[str] = []
    for row in rows:
        text = clean(row.get("message") or row.get("draft") or "")
        if text and text not in examples:
            examples.append(text[:120] + ("..." if len(text) > 120 else ""))
        if len(examples) >= limit:
            break
    return examples


def question_count(text: str) -> int:
    return text.count("?") + text.count("？")


def has_job_anchor(text: str) -> bool:
    return bool(re.search(r"岗位|机会|苏科思|士兰微|鹏新旭|微导纳米|FPGA|PQE|Device|电源|机械|硬件|质量|三次电源", text, re.I))


def bucket_for_classified(classified: dict[str, Any], text: str) -> str:
    tags = set(classified.get("reply_tags") or [])
    intent = classified.get("intent") or "unclear"
    if "确认是否在招" in tags:
        return "asks_if_open"
    if "方向确认" in tags:
        return "asks_direction"
    if intent == "need_more_info":
        return "asks_company_or_jd"
    if intent == "need_contact":
        return "contact_exchange"
    if intent == "salary_concern":
        return "salary_probe"
    if intent == "location_concern" or "地点关注" in tags:
        return "location_probe"
    if intent == "not_interested":
        return "graceful_close"
    if "自荐匹配" in tags:
        return "self_recommendation"
    if intent == "interested":
        return "positive_interest"
    if normalize_message(text) in {"您好", "你好", "hello", "hi"}:
        return "short_ping"
    return "unclear"


RULE_BLUEPRINTS = {
    "asks_if_open": {
        "category": "候选人问是否在招",
        "trigger": "还在招吗 / 目前还有在看机会吗",
        "pattern": "先回答“还在招”，补一句岗位锚点，再只问一个关键匹配问题或约 10 分钟。",
        "avoid": "不要只回“在招”，也不要一次性追问薪资、地点、动机。",
    },
    "asks_direction": {
        "category": "候选人问方向",
        "trigger": "负责什么方向 / 主要做什么 / 三次电源方向吗",
        "pattern": "直接说明客户/岗位/业务方向，随后只问是否方便继续沟通。",
        "avoid": "不要绕开方向问题，也不要先要求对方发简历。",
    },
    "asks_company_or_jd": {
        "category": "候选人要信息",
        "trigger": "哪家公司 / 工作年限要求 / JD / 岗位要求",
        "pattern": "可透露就直说客户和岗位；不可透露就说明范围，再问一个年限或方向问题。",
        "avoid": "不要含糊其辞，也不要把公司、年限、薪资、地点一起追问。",
    },
    "contact_exchange": {
        "category": "转联系方式",
        "trigger": "微信 / 手机号 / 发简历 / 联系我",
        "pattern": "我加您 + 一句机会锚点 + 微信里发岗位要点。",
        "avoid": "不要只收联系方式不说明机会；也不要在猎聘里塞长段 JD。",
    },
    "salary_probe": {
        "category": "薪资关注",
        "trigger": "薪资 / 待遇 / 总包 / 可谈",
        "pattern": "先承接薪资可对齐，只问当前大概总包区间，再判断预算匹配度。",
        "avoid": "不要替客户承诺薪资，也不要同时问固定、奖金、期望、地点四件事。",
    },
    "location_probe": {
        "category": "地点关注",
        "trigger": "苏州/上海/区域/城市/异地",
        "pattern": "先确认地点事实，再只问是否接受该城市或通勤安排。",
        "avoid": "不要先卖岗位亮点，地点不接受时继续强推。",
    },
    "self_recommendation": {
        "category": "候选人自荐",
        "trigger": "您看下合适吗 / 我很适合 / 经验匹配",
        "pattern": "先认可已收到背景，再约 10 分钟快速对齐岗位重点和经历。",
        "avoid": "不要重复让对方证明匹配，也不要马上长篇介绍。",
    },
    "positive_interest": {
        "category": "正向兴趣",
        "trigger": "感兴趣 / 希望详聊 / 可以聊 / 应聘",
        "pattern": "确认可聊，点出岗位锚点，约 10 分钟电话或微信继续。",
        "avoid": "不要只说“好的”；不要在第一条里问过多筛选问题。",
    },
    "graceful_close": {
        "category": "拒绝/不匹配",
        "trigger": "不对口 / 不是专业 / 不考虑 / 区域不接受",
        "pattern": "短句尊重反馈，保留关系：明白，有合适机会随时沟通。",
        "avoid": "不要继续解释或强推。",
    },
    "short_ping": {
        "category": "极短招呼",
        "trigger": "您好 / 你好",
        "pattern": "用一句岗位/方向锚点回应，再问是否方便了解。",
        "avoid": "不要只回您好，也不要发长篇 JD。",
    },
    "unclear": {
        "category": "不明确回复",
        "trigger": "未明确表达意图",
        "pattern": "人工复核；优先判断是否是岗位信息、地点、薪资或拒绝。",
        "avoid": "不要自动推进到外发话术。",
    },
}


def mine_rows(talk_rows: list[sqlite3.Row]) -> tuple[dict[str, list[dict[str, Any]]], Counter, Counter]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    old_strategy_counts: Counter = Counter()
    intent_counts: Counter = Counter()
    for row in talk_rows:
        message = clean(row["message"])
        direction = clean(row["direction_guess"])
        old_strategy_counts[clean(row["strategy_guess"]) or "unknown"] += 1
        if direction != "candidate" or not message:
            continue
        classified = classify_reply(message)
        intent_counts[classified["intent"]] += 1
        bucket = bucket_for_classified(classified, message)
        buckets[bucket].append(
            {
                "id": row["id"],
                "candidate": clean(row["candidate_name"]),
                "title": clean(row["candidate_title"]),
                "message": message,
                "old_strategy": clean(row["strategy_guess"]),
                "intent": classified["intent"],
                "tags": classified.get("reply_tags") or [],
            }
        )
    return buckets, old_strategy_counts, intent_counts


def mine_accepted(samples: list[sqlite3.Row], outreach_rows: list[sqlite3.Row]) -> dict[str, Any]:
    drafts = [clean(row["edited_draft"] or row["original_draft"]) for row in samples if clean(row["edited_draft"] or row["original_draft"])]
    outreach_drafts = [
        clean(row["message_summary"])
        for row in outreach_rows
        if clean(row["event_type"]) in {"reply_assistant_accept", "reply_assistant_fill"}
        and clean(row["message_summary"])
    ]
    all_drafts = list(dict.fromkeys(drafts + outreach_drafts))
    return {
        "count": len(all_drafts),
        "short_count": sum(1 for text in all_drafts if len(text) <= 55),
        "one_question_count": sum(1 for text in all_drafts if question_count(text) <= 1),
        "anchor_count": sum(1 for text in all_drafts if has_job_anchor(text)),
        "examples": all_drafts[:8],
    }


def build_rules(buckets: dict[str, list[dict[str, Any]]], accepted: dict[str, Any]) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for key, blueprint in RULE_BLUEPRINTS.items():
        rows = buckets.get(key, [])
        if not rows and key == "unclear":
            continue
        rules.append(
            {
                "rule_key": f"history_{key}",
                "category": blueprint["category"],
                "trigger_text": blueprint["trigger"],
                "recommended_pattern": blueprint["pattern"],
                "avoid_pattern": blueprint["avoid"],
                "evidence_count": len(rows),
                "confidence": confidence(len(rows)),
                "examples": example_text(rows),
            }
        )
    style_count = int(accepted.get("count") or 0)
    if style_count:
        rules.extend(
            [
                {
                    "rule_key": "history_style_short",
                    "category": "历史外发风格",
                    "trigger_text": "采纳/填入话术",
                    "recommended_pattern": "优先短句，55 字以内能说清就不要扩成长段。",
                    "avoid_pattern": "不要把内部风险提示写进候选人可见话术。",
                    "evidence_count": int(accepted.get("short_count") or 0),
                    "confidence": confidence(int(accepted.get("short_count") or 0)),
                    "examples": accepted.get("examples") or [],
                },
                {
                    "rule_key": "history_style_one_question",
                    "category": "历史外发风格",
                    "trigger_text": "采纳/填入话术",
                    "recommended_pattern": "一条消息最多一个问号；需要多问时拆到电话或微信后续沟通。",
                    "avoid_pattern": "不要把薪资、地点、动机、时间一次性全问完。",
                    "evidence_count": int(accepted.get("one_question_count") or 0),
                    "confidence": confidence(int(accepted.get("one_question_count") or 0)),
                    "examples": accepted.get("examples") or [],
                },
            ]
        )
    return rules


def save_rules(conn: sqlite3.Connection, rules: list[dict[str, Any]]) -> None:
    for rule in rules:
        conn.execute(
            """
            INSERT INTO chat_history_reply_rules (
                rule_key, category, trigger_text, recommended_pattern, avoid_pattern,
                evidence_count, confidence, examples_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
            ON CONFLICT(rule_key) DO UPDATE SET
                category = excluded.category,
                trigger_text = excluded.trigger_text,
                recommended_pattern = excluded.recommended_pattern,
                avoid_pattern = excluded.avoid_pattern,
                evidence_count = excluded.evidence_count,
                confidence = excluded.confidence,
                examples_json = excluded.examples_json,
                updated_at = datetime('now','localtime')
            """,
            (
                rule["rule_key"],
                rule["category"],
                rule["trigger_text"],
                rule["recommended_pattern"],
                rule["avoid_pattern"],
                int(rule["evidence_count"]),
                int(rule["confidence"]),
                json.dumps(rule.get("examples") or [], ensure_ascii=False),
            ),
        )
    conn.commit()


def write_report(
    output_dir: Path,
    talk_rows: list[sqlite3.Row],
    buckets: dict[str, list[dict[str, Any]]],
    old_strategy_counts: Counter,
    intent_counts: Counter,
    accepted: dict[str, Any],
    rules: list[dict[str, Any]],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘历史职聊话术挖掘_{stamp}.md"
    candidate_rows = [row for row in talk_rows if clean(row["direction_guess"]) == "candidate"]
    reclassified = sum(1 for rows in buckets.values() for row in rows if row.get("old_strategy") == "other" and row.get("intent") != "unclear")
    lines = [
        "# 猎聘历史职聊话术挖掘",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 数据概览",
        "",
        f"- 历史职聊片段：{len(talk_rows)} 条，其中候选人消息 {len(candidate_rows)} 条。",
        f"- 旧分类为 other 但可重新识别的候选人消息：{reclassified} 条。",
        f"- 已采纳/填入话术样本：{accepted.get('count', 0)} 条，短句 {accepted.get('short_count', 0)} 条，0-1 个问题 {accepted.get('one_question_count', 0)} 条。",
        f"- 旧策略分布：{'、'.join(f'{k} {v}' for k, v in old_strategy_counts.most_common()) or '暂无'}。",
        f"- 新意图分布：{'、'.join(f'{k} {v}' for k, v in intent_counts.most_common()) or '暂无'}。",
        "",
        "## 场景拆解",
        "",
        "| 场景 | 数量 | 示例 |",
        "|---|---:|---|",
    ]
    for key, rows in sorted(buckets.items(), key=lambda item: len(item[1]), reverse=True):
        blueprint = RULE_BLUEPRINTS.get(key, RULE_BLUEPRINTS["unclear"])
        examples = " / ".join(example_text(rows, 3)) or "暂无"
        lines.append(f"| {blueprint['category']} | {len(rows)} | {examples.replace('|', '｜')} |")

    lines.extend(
        [
            "",
            "## 已写入算法规则",
            "",
            "| 规则 | 证据 | 置信 | 推荐写法 | 避免 |",
            "|---|---:|---:|---|---|",
        ]
    )
    for rule in rules:
        lines.append(
            "| {category} | {count} | {confidence}/5 | {pattern} | {avoid} |".format(
                category=rule["category"].replace("|", "｜"),
                count=rule["evidence_count"],
                confidence=rule["confidence"],
                pattern=rule["recommended_pattern"].replace("|", "｜"),
                avoid=rule["avoid_pattern"].replace("|", "｜"),
            )
        )

    lines.extend(
        [
            "",
            "## 直接给话术算法的改动",
            "",
            "1. “还在招吗/目前还有吗”单独成类，优先回答在招和岗位锚点。",
            "2. “很感兴趣/希望详聊/应聘”统一视为正向窗口，优先约 10 分钟或转微信。",
            "3. “负责什么方向/三次电源方向吗”先回答方向，不先反问。",
            "4. “微信/手机号/简历”按转联系方式处理：我加您 + 机会锚点 + 微信里发岗位要点。",
            "5. 拒绝/不对口继续走短句收口，不追问、不强推。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Mine historical Liepin chat data for reply algorithm rules.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    conn = connect(Path(args.db).expanduser())
    try:
        ensure_schema(conn)
        talk_rows = load_talk_samples(conn)
        outreach_rows = load_outreach(conn)
        accepted_samples = load_samples(conn)
        buckets, old_strategy_counts, intent_counts = mine_rows(talk_rows)
        accepted = mine_accepted(accepted_samples, outreach_rows)
        rules = build_rules(buckets, accepted)
        save_rules(conn, rules)
    finally:
        conn.close()

    report = write_report(
        Path(args.output_dir).expanduser(),
        talk_rows,
        buckets,
        old_strategy_counts,
        intent_counts,
        accepted,
        rules,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "talk_samples": len(talk_rows),
                "rules": len(rules),
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
