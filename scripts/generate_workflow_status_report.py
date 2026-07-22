#!/usr/bin/env python3
"""Generate a status report for the Liepin full-workflow intelligence tree."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


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


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.OperationalError:
        return 0
    if not row:
        return 0
    return int(row[0] or 0)


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def distribution(conn: sqlite3.Connection, table: str, column: str, limit: int = 12) -> Counter:
    if not table_exists(conn, table):
        return Counter()
    result = Counter()
    for row in rows(
        conn,
        f"""
        SELECT COALESCE(NULLIF({column}, ''), '未标') AS label, COUNT(*) AS count
        FROM {table}
        GROUP BY COALESCE(NULLIF({column}, ''), '未标')
        ORDER BY count DESC
        LIMIT ?
        """,
        (limit,),
    ):
        result[str(row["label"])] = int(row["count"])
    return result


def label_counts(counter: Counter) -> str:
    if not counter:
        return "暂无"
    return "、".join(f"{key} {value}" for key, value in counter.items())


def collect_metrics(conn: sqlite3.Connection) -> dict:
    tables = [
        "candidates",
        "positions",
        "position_profiles",
        "candidate_profiles",
        "candidate_intelligence",
        "outreach_events",
        "candidate_replies",
        "followup_tasks",
        "search_experiments",
        "strategy_corrections",
        "client_feedback_events",
        "learning_notes",
        "talk_samples",
        "talk_draft_audits",
        "reply_assistant_samples",
        "reply_learning_rules",
    ]
    counts = {
        table: scalar(conn, f"SELECT COUNT(*) FROM {table}") if table_exists(conn, table) else 0
        for table in tables
    }
    open_followups = scalar(
        conn,
        "SELECT COUNT(*) FROM followup_tasks WHERE COALESCE(status, 'open')='open'",
    )
    positive_replies = scalar(
        conn,
        """
        SELECT COUNT(*) FROM candidate_replies
        WHERE intent IN ('interested', 'need_contact', 'need_more_info', 'salary_concern')
        """,
    )
    high_confidence_tasks = scalar(
        conn,
        """
        SELECT COUNT(*) FROM followup_tasks
        WHERE COALESCE(status, 'open')='open'
          AND COALESCE(match_confidence, '') IN ('confirmed', 'high', 'medium')
        """,
    )
    accepted_changed = scalar(
        conn,
        "SELECT COUNT(*) FROM reply_assistant_samples WHERE changed=1",
    )
    positive_feedback = scalar(
        conn,
        """
        SELECT COUNT(*) FROM client_feedback_events
        WHERE feedback_type IN ('approved', 'interviewing', 'interview_passed', 'offer', 'hired')
        """,
    )
    negative_feedback = scalar(
        conn,
        """
        SELECT COUNT(*) FROM client_feedback_events
        WHERE feedback_type IN ('rejected', 'interview_failed', 'eliminated')
        """,
    )
    return {
        "counts": counts,
        "open_followups": open_followups,
        "positive_replies": positive_replies,
        "high_confidence_tasks": high_confidence_tasks,
        "accepted_changed": accepted_changed,
        "positive_feedback": positive_feedback,
        "negative_feedback": negative_feedback,
        "reply_intents": distribution(conn, "candidate_replies", "intent"),
        "task_confidence": distribution(conn, "followup_tasks", "match_confidence"),
        "task_status": distribution(conn, "followup_tasks", "status"),
        "talk_strategy": distribution(conn, "candidate_replies", "talk_strategy"),
        "assistant_strategy": distribution(conn, "reply_assistant_samples", "strategy_key"),
        "candidate_status": distribution(conn, "candidates", "status"),
        "search_status": distribution(conn, "search_experiments", "status"),
        "client_feedback_type": distribution(conn, "client_feedback_events", "feedback_type"),
    }


def status_for_modules(metrics: dict) -> list[dict[str, str]]:
    counts = metrics["counts"]
    modules: list[dict[str, str]] = []

    modules.append(
        {
            "module": "A 数据底座",
            "status": "已搭好",
            "evidence": (
                f"核心表已存在；回复 {counts['candidate_replies']} 条、待办 {counts['followup_tasks']} 条、"
                f"客户反馈 {counts['client_feedback_events']} 条、学习记录 {counts['learning_notes']} 条。"
            ),
            "gap": "候选人评分、触达事件、搜索实验和客户反馈仍需要持续补真实数据。",
        }
    )
    modules.append(
        {
            "module": "B 候选人智能判断",
            "status": "结构化可用",
            "evidence": (
                f"岗位画像 {counts['position_profiles']} 个、候选人画像 {counts['candidate_profiles']} 个；"
                f"candidate_intelligence 当前 {counts['candidate_intelligence']} 条。"
            ),
            "gap": "后续可接入更细的薪资、城市迁移、跳槽频率等字段。",
        }
    )
    modules.append(
        {
            "module": "C 触达智能",
            "status": "可写回/可校验",
            "evidence": (
                f"回复助手可生成专业话术，推荐前校验已接入刷新；"
                f"采纳样本 {counts['reply_assistant_samples']} 条，话术学习规则 {counts['reply_learning_rules']} 条，"
                f"outreach_events 当前 {counts['outreach_events']} 条。"
            ),
            "gap": "需要继续积累真实触达事件，后续才能稳定计算不同话术的转化。",
        }
    )
    modules.append(
        {
            "module": "D 回复理解",
            "status": "基本可用",
            "evidence": (
                f"候选人回复 {counts['candidate_replies']} 条，正向/可继续 {metrics['positive_replies']} 条，"
                f"打开待办 {metrics['open_followups']} 条；工作台已支持手动粘贴回复并生成待办。"
            ),
            "gap": "低置信项目仍要补客户/岗位确认。",
        }
    )
    modules.append(
        {
            "module": "E 策略学习",
            "status": "可记录/可出建议",
            "evidence": (
                f"search_experiments 当前 {counts['search_experiments']} 条，"
                f"策略修正规则 {counts['strategy_corrections']} 条，话术历史样本 {counts['talk_samples']} 条。"
            ),
            "gap": "需要在真实搜索后持续回填查看数、入库数、推荐数和回复数。",
        }
    )
    modules.append(
        {
            "module": "F 客户反馈闭环",
            "status": "可记录/待积累",
            "evidence": (
                f"client_feedback_events 当前 {counts['client_feedback_events']} 条；"
                f"正向 {metrics['positive_feedback']} 条、负向 {metrics['negative_feedback']} 条。"
            ),
            "gap": "下一步需要把真实客户认可、否决和面试结果持续写回。",
        }
    )
    modules.append(
        {
            "module": "G 工作台与提醒",
            "status": "全链路可用",
            "evidence": "工作台已支持客户反馈、搜索实验、项目归属、候选人回复、触达事件、候选人状态、关闭待办等写回，并新增岗位详情页；回复助手可直连同步采纳样本和触达事件。",
            "gap": "后续主要靠真实数据积累和细化字段，不再缺主链路入口。",
        }
    )
    return modules


def next_round_recommendations(metrics: dict) -> list[str]:
    counts = metrics["counts"]
    suggestions: list[str] = []
    if counts["search_experiments"] == 0:
        suggestions.append(
            "优先做 E1：把每轮猎聘搜索记录成实验，包括关键词、筛选、结果数、入围数、推荐数和回复数。"
        )
    else:
        suggestions.append(
            "继续做 E2：定期看搜索实验复盘，保留高转化关键词，给噪音关键词加限制或降权。"
        )
    if counts["outreach_events"] == 0:
        suggestions.append(
            "补 C3：回复助手或推荐动作发生后，至少记录一次触达事件，后面才能算话术/岗位转化。"
        )
    if counts["candidate_intelligence"] == 0:
        suggestions.append(
            "补 B3：先给当前打开待办和高意向人选生成基线匹配评分、风险点、验证问题。"
        )
    if counts["client_feedback_events"] == 0:
        suggestions.append(
            "补 F1：客户有认可、否决、面试或 offer 结果时，双击 `记录客户反馈.command` 写回，系统会自动影响画像和下一轮搜索策略。"
        )
    else:
        suggestions.append(
            "推进 F3：用客户正负反馈复盘岗位画像，正样本扩搜、负样本降噪。"
        )
    if metrics["accepted_changed"]:
        suggestions.append(
            f"继续利用 {metrics['accepted_changed']} 条人工改写样本升级话术规则，重点学习“更短、少问题、先说机会”。"
        )
    if not suggestions:
        suggestions.append("进入岗位级驾驶舱，把搜索、触达、回复、客户反馈汇到同一个岗位推进页。")
    return suggestions


def write_report(output_dir: Path, metrics: dict) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘全流程主任务树推进报告_{stamp}.md"
    modules = status_for_modules(metrics)
    suggestions = next_round_recommendations(metrics)
    counts = metrics["counts"]

    lines = [
        "# 猎聘全流程主任务树推进报告",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 一句话结论",
        "",
        "猎聘全流程智能化主链路已经具备闭环入口：画像、推荐前校验、触达、回复、搜索实验、客户反馈、策略修正和工作台写回都已串起来。",
        "",
        "## 当前数据概览",
        "",
        "| 数据项 | 数量 | 说明 |",
        "|---|---:|---|",
        f"| 候选人 | {counts['candidates']} | 本地人才库总候选人 |",
        f"| 岗位 | {counts['positions']} | 本地岗位池 |",
        f"| 岗位画像 | {counts['position_profiles']} | 标准化岗位门槛、能力、关键词 |",
        f"| 候选人画像 | {counts['candidate_profiles']} | 标准化候选人能力、层级、风险 |",
        f"| 候选人回复 | {counts['candidate_replies']} | 已抓取并分类的猎聘回复 |",
        f"| 跟进待办 | {counts['followup_tasks']} | 回复后生成的动作 |",
        f"| 打开待办 | {metrics['open_followups']} | 当前仍需处理 |",
        f"| 历史话术样本 | {counts['talk_samples']} | 从猎聘历史沟通收集 |",
        f"| 回复助手采纳样本 | {counts['reply_assistant_samples']} | 你改好并采纳的样本 |",
        f"| 话术学习规则 | {counts['reply_learning_rules']} | 从采纳样本和候选人回复沉淀 |",
        f"| 搜索实验 | {counts['search_experiments']} | 每轮关键词/筛选/结果记录 |",
        f"| 策略修正规则 | {counts['strategy_corrections']} | 由搜索实验和客户反馈反推 |",
        f"| 客户反馈 | {counts['client_feedback_events']} | 客户认可/否决/面试/offer 结果 |",
        f"| 触达事件 | {counts['outreach_events']} | 推荐/开聊/填入等动作记录 |",
        f"| 候选人智能画像 | {counts['candidate_intelligence']} | 人岗匹配评分与风险 |",
        "",
        "## 模块状态",
        "",
        "| 模块 | 状态 | 已有证据 | 当前缺口 |",
        "|---|---|---|---|",
    ]
    for module in modules:
        lines.append(
            f"| {module['module']} | {module['status']} | {module['evidence']} | {module['gap']} |"
        )

    lines.extend(
        [
            "",
            "## 关键分布",
            "",
            f"- 回复意图：{label_counts(metrics['reply_intents'])}",
            f"- 待办置信度：{label_counts(metrics['task_confidence'])}",
            f"- 待办状态：{label_counts(metrics['task_status'])}",
            f"- 回复话术策略：{label_counts(metrics['talk_strategy'])}",
            f"- 插件采纳策略：{label_counts(metrics['assistant_strategy'])}",
            f"- 搜索实验状态：{label_counts(metrics['search_status'])}",
            f"- 客户反馈类型：{label_counts(metrics['client_feedback_type'])}",
            f"- 候选人状态：{label_counts(metrics['candidate_status'])}",
            "",
            "## 下一轮推进建议",
            "",
        ]
    )
    for idx, suggestion in enumerate(suggestions, start=1):
        lines.append(f"{idx}. {suggestion}")

    lines.extend(
        [
            "",
            "## 建议的执行顺序",
            "",
            "1. 先补搜索实验记录，让每次猎聘搜索都可复盘。",
            "2. 客户有反馈时立刻写回，正负样本会影响人选画像和下一轮搜索策略。",
            "3. 然后用猎聘智能工作台直接写回触达、待办、客户反馈、搜索实验和项目归属。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Liepin workflow status report.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    conn = connect(db_path)
    try:
        metrics = collect_metrics(conn)
    finally:
        conn.close()

    report = write_report(output_dir, metrics)
    print(
        json.dumps(
            {
                "ok": True,
                "db": str(db_path),
                "report": str(report),
                "counts": metrics["counts"],
                "next": next_round_recommendations(metrics),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
