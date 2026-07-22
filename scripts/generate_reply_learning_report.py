#!/usr/bin/env python3
"""Generate learned reply style and classification rules from real usage."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from reply_intelligence_rules import classify_reply


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


RULE_SCHEMA = """
CREATE TABLE IF NOT EXISTS reply_learning_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_key TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    rule_text TEXT NOT NULL,
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


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(RULE_SCHEMA)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reply_learning_rules_category ON reply_learning_rules(category)")
    if table_exists(conn, "candidate_replies"):
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(candidate_replies)")}
        extras = {
            "reply_tags_json": "TEXT DEFAULT '[]'",
            "classification_reason": "TEXT",
            "classifier_version": "TEXT",
        }
        for column, definition in extras.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE candidate_replies ADD COLUMN {column} {definition}")
    conn.commit()


def clean(text: Any) -> str:
    return " ".join(str(text or "").split())


def load_rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def load_samples(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not table_exists(conn, "reply_assistant_samples"):
        return []
    return load_rows(
        conn,
        """
        SELECT accepted_at, candidate_name, candidate_title, latest_message,
               strategy_key, strategy_label, score, grade, project_client,
               project_position, original_draft, edited_draft, changed,
               length_delta, reasons_json, missing_json, risk_json
        FROM reply_assistant_samples
        ORDER BY datetime(accepted_at) DESC, synced_at DESC
        """,
    )


def load_replies(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not table_exists(conn, "candidate_replies"):
        return []
    return load_rows(
        conn,
        """
        SELECT candidate_name, candidate_title, client, position, raw_text,
               intent, sentiment, blockers_json, suggested_next_action,
               reply_tags_json, classification_reason, classifier_version,
               created_at
        FROM candidate_replies
        ORDER BY id DESC
        LIMIT 200
        """,
    )


def load_history_rules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not table_exists(conn, "chat_history_reply_rules"):
        return []
    return load_rows(
        conn,
        """
        SELECT rule_key, category, trigger_text, recommended_pattern,
               avoid_pattern, evidence_count, confidence, examples_json
        FROM chat_history_reply_rules
        ORDER BY evidence_count DESC, confidence DESC, rule_key
        """,
    )


def load_context_rules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not table_exists(conn, "conversation_context_reply_rules"):
        return []
    return load_rows(
        conn,
        """
        SELECT rule_key, category, trigger_text, recommended_pattern,
               avoid_pattern, evidence_count, confidence, examples_json
        FROM conversation_context_reply_rules
        ORDER BY evidence_count DESC, confidence DESC, rule_key
        """,
    )


def backfill_reply_classification(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "candidate_replies"):
        return 0
    rows = load_rows(
        conn,
        """
        SELECT id, raw_text
        FROM candidate_replies
        WHERE COALESCE(reply_tags_json, '') IN ('', '[]')
           OR COALESCE(classification_reason, '') = ''
           OR COALESCE(classifier_version, '') = ''
        """,
    )
    updates = 0
    for row in rows:
        classified = classify_reply(row["raw_text"] or "")
        conn.execute(
            """
            UPDATE candidate_replies
            SET intent = CASE WHEN COALESCE(intent, '') IN ('', 'unclear') THEN ? ELSE intent END,
                sentiment = CASE WHEN COALESCE(sentiment, '') IN ('', 'neutral') THEN ? ELSE sentiment END,
                blockers_json = CASE WHEN COALESCE(blockers_json, '') IN ('', '[]') THEN ? ELSE blockers_json END,
                reply_tags_json = ?,
                classification_reason = ?,
                classifier_version = ?,
                suggested_next_action = CASE WHEN COALESCE(suggested_next_action, '') = '' THEN ? ELSE suggested_next_action END,
                processed_at = COALESCE(processed_at, datetime('now','localtime'))
            WHERE id = ?
            """,
            (
                classified["intent"],
                classified["sentiment"],
                json.dumps(classified["blockers"], ensure_ascii=False),
                json.dumps(classified.get("reply_tags") or [], ensure_ascii=False),
                classified.get("classification_reason") or "",
                classified.get("classifier_version") or "",
                classified["suggested_next_action"],
                row["id"],
            ),
        )
        updates += 1
    conn.commit()
    return updates


def count_questions(text: str) -> int:
    return text.count("?") + text.count("？")


def has_any(text: str, words: list[str]) -> bool:
    return any(word in text for word in words)


def examples(rows: list[sqlite3.Row], field: str, limit: int = 3) -> list[str]:
    result: list[str] = []
    for row in rows:
        value = clean(row[field])
        if value and value not in result:
            result.append(value[:160] + ("..." if len(value) > 160 else ""))
        if len(result) >= limit:
            break
    return result


def confidence_from_count(count: int) -> int:
    if count >= 12:
        return 5
    if count >= 6:
        return 4
    if count >= 2:
        return 3
    return 2 if count else 1


def build_rules(
    samples: list[sqlite3.Row],
    replies: list[sqlite3.Row],
    history_rules: list[sqlite3.Row],
    context_rules: list[sqlite3.Row],
) -> list[dict[str, Any]]:
    changed = [row for row in samples if int(row["changed"] or 0)]
    edited = [row for row in samples if clean(row["edited_draft"])]
    shorter = [row for row in changed if int(row["length_delta"] or 0) < 0]
    one_question = [row for row in edited if count_questions(clean(row["edited_draft"])) <= 1]
    next_action = [
        row for row in edited
        if has_any(clean(row["edited_draft"]), ["电话", "微信", "沟通", "确认", "方便"])
    ]
    project_context = [
        row for row in edited
        if has_any(clean(row["edited_draft"]), ["岗位", "客户", clean(row["project_position"])])
    ]
    salutation_rows = [
        row for row in edited
        if has_any(clean(row["edited_draft"])[:12], ["工", "总", "老师"])
    ]
    positive_replies = [
        row for row in replies
        if clean(row["intent"]) in {"interested", "need_contact", "need_more_info", "salary_concern"}
    ]

    rules = [
        {
            "rule_key": "style_short_direct",
            "category": "话术风格",
            "rule_text": "优先短句、直给机会信息，减少铺垫。候选人愿意聊时直接进入电话/微信/关键条件确认。",
            "evidence_count": len(shorter),
            "confidence": confidence_from_count(len(shorter)),
            "examples": examples(shorter, "edited_draft"),
        },
        {
            "rule_key": "style_one_question",
            "category": "话术风格",
            "rule_text": "一条回复里尽量只放 0-1 个问号，不把薪资、地点、动机、时间一次性全问出去。",
            "evidence_count": len(one_question),
            "confidence": confidence_from_count(len(one_question)),
            "examples": examples(one_question, "edited_draft"),
        },
        {
            "rule_key": "style_project_context",
            "category": "话术风格",
            "rule_text": "回复里要保留岗位/客户/方向中的至少一个锚点，让候选人知道这不是群发。",
            "evidence_count": len(project_context),
            "confidence": confidence_from_count(len(project_context)),
            "examples": examples(project_context, "edited_draft"),
        },
        {
            "rule_key": "style_next_action",
            "category": "推进动作",
            "rule_text": "正向回复后要给清晰下一步：加微信、约 10 分钟电话、或先确认一个关键条件。",
            "evidence_count": len(next_action),
            "confidence": confidence_from_count(len(next_action)),
            "examples": examples(next_action, "edited_draft"),
        },
        {
            "rule_key": "salutation_title_based",
            "category": "称呼规则",
            "rule_text": "工程师/专家类称呼优先“姓氏+工”；经理、总监、副总、负责人类优先“姓氏+总”；不确定时用“姓氏+老师”。",
            "evidence_count": max(len(salutation_rows), 1),
            "confidence": 5,
            "examples": examples(salutation_rows, "edited_draft"),
        },
        {
            "rule_key": "reply_positive_fast_follow",
            "category": "回复分类",
            "rule_text": "有兴趣、要信息、问薪资、愿意加微信都视为可继续沟通，优先当天处理。",
            "evidence_count": len(positive_replies),
            "confidence": confidence_from_count(len(positive_replies)),
            "examples": examples(positive_replies, "raw_text"),
        },
    ]
    for row in history_rules:
        rules.append(
            {
                "rule_key": clean(row["rule_key"]),
                "category": clean(row["category"]) or "历史职聊规则",
                "rule_text": clean(row["recommended_pattern"]),
                "evidence_count": int(row["evidence_count"] or 0),
                "confidence": int(row["confidence"] or 3),
                "examples": parse_examples(row["examples_json"]),
            }
        )
    for row in context_rules:
        rules.append(
            {
                "rule_key": clean(row["rule_key"]),
                "category": clean(row["category"]) or "真实对话规则",
                "rule_text": clean(row["recommended_pattern"]),
                "evidence_count": int(row["evidence_count"] or 0),
                "confidence": int(row["confidence"] or 3),
                "examples": parse_examples(row["examples_json"]),
            }
        )
    return rules


def parse_examples(text: str | None) -> list[str]:
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [clean(item) for item in value if clean(item)][:3]


def save_rules(conn: sqlite3.Connection, rules: list[dict[str, Any]]) -> None:
    for rule in rules:
        conn.execute(
            """
            INSERT INTO reply_learning_rules (
                rule_key, category, rule_text, evidence_count,
                confidence, examples_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'))
            ON CONFLICT(rule_key) DO UPDATE SET
                category = excluded.category,
                rule_text = excluded.rule_text,
                evidence_count = excluded.evidence_count,
                confidence = excluded.confidence,
                examples_json = excluded.examples_json,
                updated_at = datetime('now','localtime')
            """,
            (
                rule["rule_key"],
                rule["category"],
                rule["rule_text"],
                int(rule["evidence_count"]),
                int(rule["confidence"]),
                json.dumps(rule.get("examples") or [], ensure_ascii=False),
            ),
        )
    conn.commit()


def distribution(rows: list[sqlite3.Row], field: str) -> Counter:
    result: Counter = Counter()
    for row in rows:
        label = clean(row[field]) or "未标"
        result[label] += 1
    return result


def label_counts(counter: Counter) -> str:
    if not counter:
        return "暂无"
    return "、".join(f"{key} {value}" for key, value in counter.most_common())


def write_report(
    output_dir: Path,
    samples: list[sqlite3.Row],
    replies: list[sqlite3.Row],
    rules: list[dict[str, Any]],
    history_rules: list[sqlite3.Row],
    context_rules: list[sqlite3.Row],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘话术学习器_{stamp}.md"
    changed = [row for row in samples if int(row["changed"] or 0)]
    deltas = [int(row["length_delta"] or 0) for row in samples]
    avg_delta = mean(deltas) if deltas else 0
    intent_counts = distribution(replies, "intent")
    strategy_counts = distribution(samples, "strategy_key")

    lines = [
        "# 猎聘话术学习器",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 当前学习结论",
        "",
        f"- 采纳样本：{len(samples)} 条，其中人工修改：{len(changed)} 条。",
        f"- 平均长度变化：{avg_delta:.1f} 字；负数代表你倾向把草稿压短。",
        f"- 候选人回复：{len(replies)} 条，意图分布：{label_counts(intent_counts)}。",
        f"- 插件策略分布：{label_counts(strategy_counts)}。",
        f"- 历史职聊规则：{len(history_rules)} 条，来自过往职聊片段挖掘。",
        f"- 真实上下文规则：{len(context_rules)} 条，来自 Chrome 会话往返学习。",
        "",
        "## 已沉淀规则",
        "",
        "| 类别 | 规则 | 证据 | 置信 | 示例 |",
        "|---|---|---:|---:|---|",
    ]
    for rule in rules:
        ex = " / ".join(rule.get("examples") or []) or "暂无样例"
        lines.append(
            "| {category} | {text} | {count} | {confidence}/5 | {examples} |".format(
                category=rule["category"].replace("|", "｜"),
                text=rule["rule_text"].replace("|", "｜"),
                count=rule["evidence_count"],
                confidence=rule["confidence"],
                examples=ex.replace("|", "｜"),
            )
        )

    lines.extend(
        [
            "",
            "## 历史职聊新增洞察",
            "",
        ]
    )
    if history_rules:
        lines.append("| 场景 | 证据 | 推荐写法 | 避免 |")
        lines.append("|---|---:|---|---|")
        for row in history_rules[:10]:
            lines.append(
                "| {category} | {count} | {pattern} | {avoid} |".format(
                    category=clean(row["category"]).replace("|", "｜"),
                    count=int(row["evidence_count"] or 0),
                    pattern=clean(row["recommended_pattern"]).replace("|", "｜"),
                    avoid=clean(row["avoid_pattern"]).replace("|", "｜"),
                )
            )
    else:
        lines.append("- 暂无历史职聊规则；先运行历史职聊挖掘。")

    lines.extend(
        [
            "",
            "## 真实对话新增洞察",
            "",
        ]
    )
    if context_rules:
        lines.append("| 场景 | 证据 | 推荐写法 | 避免 |")
        lines.append("|---|---:|---|---|")
        for row in context_rules[:10]:
            lines.append(
                "| {category} | {count} | {pattern} | {avoid} |".format(
                    category=clean(row["category"]).replace("|", "｜"),
                    count=int(row["evidence_count"] or 0),
                    pattern=clean(row["recommended_pattern"]).replace("|", "｜"),
                    avoid=clean(row["avoid_pattern"]).replace("|", "｜"),
                )
            )
    else:
        lines.append("- 暂无真实对话规则；先运行 Chrome 上下文采样与挖掘。")

    lines.extend(
        [
            "",
            "## 回复分类算法",
            "",
            "| 意图 | 处理方式 |",
            "|---|---|",
            "| interested | 当天跟进，补岗位信息并约 10 分钟电话。 |",
            "| need_more_info | 先补公司/岗位/JD 核心信息，再引导电话确认。 |",
            "| need_contact | 承接微信/电话，同时确认岗位方向、地点、薪资。 |",
            "| salary_concern | 只确认当前和期望区间，不提前替客户承诺。 |",
            "| location_concern | 先确认地点接受度、通勤和家庭约束。 |",
            "| not_interested | 尊重反馈，记录原因；有更贴合岗位再沟通。 |",
            "| unclear | 进入人工复核，避免误判和乱回。 |",
            "",
            "## 最新候选人回复样本",
            "",
        ]
    )
    if replies:
        lines.append("| 候选人 | 意图 | 标签 | 建议动作 | 原话 |")
        lines.append("|---|---|---|---|---|")
        for row in replies[:10]:
            try:
                tags = json.loads(row["reply_tags_json"] or "[]")
            except json.JSONDecodeError:
                tags = []
            lines.append(
                "| {name} | {intent} | {tags} | {action} | {raw} |".format(
                    name=clean(row["candidate_name"]).replace("|", "｜") or "未识别",
                    intent=clean(row["intent"]).replace("|", "｜") or "未标",
                    tags=("、".join(tags) if tags else "无").replace("|", "｜"),
                    action=clean(row["suggested_next_action"]).replace("|", "｜")[:80],
                    raw=clean(row["raw_text"]).replace("|", "｜")[:90],
                )
            )
    else:
        lines.append("- 暂无候选人回复。")

    lines.extend(
        [
            "",
            "## 下一步",
            "",
            "1. 在猎聘回复助手里继续点击“采纳修改”，系统会学习你最后发出去的版本。",
            "2. 遇到关键候选人回复时，可在工作台粘贴记录，系统会立刻分类并生成待办。",
            "3. 后续真实触达和客户反馈越多，话术规则会从“风格偏好”升级为“转化偏好”。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Liepin reply learning report.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    conn = connect(db_path)
    try:
        ensure_schema(conn)
        backfilled = backfill_reply_classification(conn)
        samples = load_samples(conn)
        replies = load_replies(conn)
        history_rules = load_history_rules(conn)
        context_rules = load_context_rules(conn)
        rules = build_rules(samples, replies, history_rules, context_rules)
        save_rules(conn, rules)
    finally:
        conn.close()

    report = write_report(output_dir, samples, replies, rules, history_rules, context_rules)
    print(
        json.dumps(
            {
                "ok": True,
                "samples": len(samples),
                "replies": len(replies),
                "rules": len(rules),
                "history_rules": len(history_rules),
                "context_rules": len(context_rules),
                "backfilled_replies": backfilled,
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
