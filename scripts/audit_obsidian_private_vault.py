#!/usr/bin/env python3
"""Audit Liepin's private Obsidian vault and SQLite execution cache.

The audit focuses on whether the private vault can drive daily recruiting
actions: project ownership, candidate state, open follow-ups, replies, matching
coverage, role health, duplicate risk, and privacy boundaries.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_PRIVATE_VAULT = Path.home() / "Documents" / "Obsidian Liepin Private Vault"
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"

AUTO_START = "<!-- AUTO_SYNC_START -->"
AUTO_END = "<!-- AUTO_SYNC_END -->"
MANUAL_SECTION = "## 人工补充\n\n- \n"

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)\b(cookie|authorization|bearer token|api[_ -]?key|password|passwd|secret)\s*[:=]"),
    re.compile(r"https?://h\.liepin\.com/\S+"),
    re.compile(r"\b(?:res_id_encode|ck_id|sk_id|fk_id|skId|ckId|fkId)=[^&\s|]+"),
]

ACTIONABLE_REPLY_INTENTS = {
    "short_confirmation",
    "self_recommendation",
    "need_more_info",
    "targeted_interest",
    "location_concern",
    "need_contact",
    "salary_concern",
}
ACTIVE_STATUSES = {"new", "greeted", "contacted", "replied", "recommended", "client_approved", "interviewing", "offered"}
CLOSED_STATUSES = {"hired", "eliminated", "passed", "client_rejected", "duplicate"}


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


def rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def clean(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\u3000", " ").split())


def short(value: Any, limit: int = 80) -> str:
    text = clean(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def row_get(row: sqlite3.Row | dict[str, Any] | None, key: str, default: Any = "") -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    return row[key] if key in row.keys() else default


def parse_time(value: Any) -> datetime | None:
    text = clean(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19] if "T" in text else text[: len(fmt)], fmt)
        except ValueError:
            continue
    return None


def md_table(headers: list[str], data_rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for data_row in data_rows:
        cells = [short(cell, 120).replace("|", "\\|") if cell not in (None, "") else "未填" for cell in data_row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def bullet(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- 暂无"


def note_frontmatter(title: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    return "\n".join(
        [
            "---",
            f'title: "{title}"',
            f"created: {today}",
            f"updated: {today}",
            "type: review",
            "tags: [liepin, private, audit, data-quality]",
            "---",
        ]
    )


def replace_auto_block(path: Path, title: str, body: str, dry_run: bool) -> None:
    block = f"{AUTO_START}\n{body.rstrip()}\n{AUTO_END}"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if AUTO_START in existing and AUTO_END in existing:
            prefix, rest = existing.split(AUTO_START, 1)
            _, suffix = rest.split(AUTO_END, 1)
            content = prefix.rstrip() + "\n\n" + block + suffix
        else:
            content = existing.rstrip() + "\n\n" + block + "\n"
    else:
        content = f"{note_frontmatter(title)}\n\n# {title}\n\n{block}\n\n{MANUAL_SECTION}"
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def privacy_findings(vault: Path) -> list[str]:
    findings: list[str] = []
    if not vault.exists():
        return [f"私密库不存在：{vault}"]
    for path in vault.rglob("*.md"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(str(path.relative_to(vault)))
                break
    return findings


def candidate_note_missing(vault: Path, candidate_ids: list[int]) -> list[int]:
    if not vault.exists():
        return candidate_ids
    candidate_dir = vault / "30_Candidates"
    missing: list[int] = []
    for candidate_id in candidate_ids:
        if not list(candidate_dir.glob(f"candidate-{candidate_id}-*.md")):
            missing.append(candidate_id)
    return missing


def build_audit(conn: sqlite3.Connection, private_vault: Path) -> dict[str, Any]:
    candidates = rows(conn, "SELECT * FROM candidates ORDER BY id") if table_exists(conn, "candidates") else []
    profiles = rows(conn, "SELECT * FROM candidate_profiles") if table_exists(conn, "candidate_profiles") else []
    intelligence = rows(conn, "SELECT * FROM candidate_intelligence") if table_exists(conn, "candidate_intelligence") else []
    replies = rows(conn, "SELECT * FROM candidate_replies") if table_exists(conn, "candidate_replies") else []
    tasks = rows(conn, "SELECT * FROM followup_tasks") if table_exists(conn, "followup_tasks") else []
    outreach = rows(conn, "SELECT * FROM outreach_events") if table_exists(conn, "outreach_events") else []
    searches = rows(conn, "SELECT * FROM search_experiments") if table_exists(conn, "search_experiments") else []
    feedback = rows(conn, "SELECT * FROM client_feedback_events") if table_exists(conn, "client_feedback_events") else []
    positions = rows(conn, "SELECT * FROM positions") if table_exists(conn, "positions") else []
    position_profiles = rows(conn, "SELECT * FROM position_profiles") if table_exists(conn, "position_profiles") else []

    counts = {
        "candidates": len(candidates),
        "candidate_profiles": len(profiles),
        "candidate_intelligence": len(intelligence),
        "candidate_replies": len(replies),
        "followup_tasks": len(tasks),
        "outreach_events": len(outreach),
        "search_experiments": len(searches),
        "client_feedback_events": len(feedback),
        "positions": len(positions),
        "position_profiles": len(position_profiles),
    }

    candidate_ids = [int(row["id"]) for row in candidates if row_get(row, "id")]
    profile_ids = {int(row["candidate_id"]) for row in profiles if row_get(row, "candidate_id")}
    intelligence_keys = {
        (clean(row_get(row, "candidate_name")), clean(row_get(row, "candidate_company")), clean(row_get(row, "client")), clean(row_get(row, "position")))
        for row in intelligence
    }
    candidate_keys = {
        int(row["id"]): (clean(row_get(row, "name")), clean(row_get(row, "company")), clean(row_get(row, "client")), clean(row_get(row, "position")))
        for row in candidates
    }

    missing_fields = {
        "client": [row for row in candidates if not clean(row_get(row, "client"))],
        "position": [row for row in candidates if not clean(row_get(row, "position"))],
        "company": [row for row in candidates if not clean(row_get(row, "company"))],
        "title": [row for row in candidates if not clean(row_get(row, "title"))],
        "education": [row for row in candidates if not clean(row_get(row, "education"))],
        "city": [row for row in candidates if not clean(row_get(row, "city"))],
        "profile": [row for row in candidates if int(row["id"]) not in profile_ids],
        "intelligence": [row for row in candidates if candidate_keys[int(row["id"])] not in intelligence_keys],
    }

    by_name_company: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    by_name: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in candidates:
        name = clean(row_get(row, "name"))
        company = clean(row_get(row, "company"))
        if name:
            by_name[name].append(row)
        if name or company:
            by_name_company[(name, company)].append(row)
    duplicate_name_company = {key: value for key, value in by_name_company.items() if len(value) > 1}
    duplicate_name = {key: value for key, value in by_name.items() if len(value) > 1}

    open_tasks = [row for row in tasks if clean(row_get(row, "status", "open")).lower() == "open"]
    overdue_tasks = []
    now = datetime.now()
    for row in open_tasks:
        due = parse_time(row_get(row, "due_at"))
        if due and due < now:
            overdue_tasks.append(row)

    unconfirmed_tasks = [
        row
        for row in open_tasks
        if clean(row_get(row, "confirmation_status", "unconfirmed")) != "confirmed"
    ]
    unmatched_tasks = [
        row
        for row in open_tasks
        if clean(row_get(row, "match_confidence", "unmatched")) in {"unmatched", "low", ""}
    ]

    task_candidate_keys = {
        (clean(row_get(row, "candidate_name")), clean(row_get(row, "candidate_company")), clean(row_get(row, "client")), clean(row_get(row, "position")))
        for row in open_tasks
    }
    actionable_replies = [row for row in replies if clean(row_get(row, "intent")) in ACTIONABLE_REPLY_INTENTS]
    replies_without_open_task = [
        row
        for row in actionable_replies
        if (
            clean(row_get(row, "candidate_name")),
            clean(row_get(row, "candidate_company")),
            clean(row_get(row, "client")),
            clean(row_get(row, "position")),
        )
        not in task_candidate_keys
    ]

    outreach_keys = {
        (clean(row_get(row, "candidate_name")), clean(row_get(row, "candidate_company")), clean(row_get(row, "client")), clean(row_get(row, "position")))
        for row in outreach
    }
    reply_keys = {
        (clean(row_get(row, "candidate_name")), clean(row_get(row, "candidate_company")), clean(row_get(row, "client")), clean(row_get(row, "position")))
        for row in replies
    }
    candidates_touched_no_reply_no_task = [
        row
        for row in candidates
        if candidate_keys[int(row["id"])] in outreach_keys
        and candidate_keys[int(row["id"])] not in reply_keys
        and candidate_keys[int(row["id"])] not in task_candidate_keys
        and clean(row_get(row, "status")).lower() not in CLOSED_STATUSES
    ]

    status_counter = Counter(clean(row_get(row, "status", "new")) or "new" for row in candidates)
    reply_intent_counter = Counter(clean(row_get(row, "intent")) or "unclear" for row in replies)
    task_status_counter = Counter(clean(row_get(row, "status", "open")) or "open" for row in tasks)
    task_lane_counter = Counter(clean(row_get(row, "lane_tag")) or "未分层" for row in open_tasks)
    search_status_counter = Counter(clean(row_get(row, "status", "open")) or "open" for row in searches)

    position_keys = {(clean(row_get(row, "client")), clean(row_get(row, "title"))) for row in positions}
    profile_position_keys = {(clean(row_get(row, "client")), clean(row_get(row, "position"))) for row in position_profiles}
    candidate_project_keys = {(clean(row_get(row, "client")), clean(row_get(row, "position"))) for row in candidates}
    search_project_keys = {(clean(row_get(row, "client")), clean(row_get(row, "position"))) for row in searches}
    intelligence_project_keys = {(clean(row_get(row, "client")), clean(row_get(row, "position"))) for row in intelligence}

    positions_without_profile = sorted(key for key in position_keys if key not in profile_position_keys)
    positions_without_candidates = sorted(key for key in position_keys if key not in candidate_project_keys)
    positions_without_search = sorted(key for key in position_keys if key not in search_project_keys)
    positions_without_intelligence = sorted(key for key in position_keys if key not in intelligence_project_keys)

    zero_result_searches = [
        row for row in searches if int(row_get(row, "extracted_count", 0) or 0) == 0 and int(row_get(row, "result_count", 0) or 0) == 0
    ]
    no_reply_searches = [
        row for row in searches if int(row_get(row, "reply_count", 0) or 0) == 0 and int(row_get(row, "extracted_count", 0) or 0) > 0
    ]

    privacy_hits = privacy_findings(private_vault)
    missing_notes = candidate_note_missing(private_vault, candidate_ids)

    critical = 0
    warnings = 0
    critical += 1 if privacy_hits else 0
    critical += 1 if missing_notes else 0
    warnings += len(missing_fields["client"]) + len(missing_fields["position"])
    warnings += len(missing_fields["intelligence"])
    warnings += len(unmatched_tasks)
    warnings += len(replies_without_open_task)
    warnings += len(positions_without_profile)

    total_candidates = max(len(candidates), 1)
    completeness_score = 100
    completeness_score -= round(len(missing_fields["client"]) / total_candidates * 25)
    completeness_score -= round(len(missing_fields["position"]) / total_candidates * 20)
    completeness_score -= round(len(missing_fields["intelligence"]) / total_candidates * 25)
    completeness_score -= min(20, round(len(unmatched_tasks) / max(len(tasks), 1) * 20))
    completeness_score -= 10 if privacy_hits or missing_notes else 0
    completeness_score = max(0, min(100, completeness_score))

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "counts": counts,
        "score": completeness_score,
        "critical": critical,
        "warnings": warnings,
        "status_counter": status_counter,
        "reply_intent_counter": reply_intent_counter,
        "task_status_counter": task_status_counter,
        "task_lane_counter": task_lane_counter,
        "search_status_counter": search_status_counter,
        "missing_fields": missing_fields,
        "duplicate_name_company": duplicate_name_company,
        "duplicate_name": duplicate_name,
        "open_tasks": open_tasks,
        "overdue_tasks": overdue_tasks,
        "unconfirmed_tasks": unconfirmed_tasks,
        "unmatched_tasks": unmatched_tasks,
        "replies_without_open_task": replies_without_open_task,
        "candidates_touched_no_reply_no_task": candidates_touched_no_reply_no_task,
        "positions_without_profile": positions_without_profile,
        "positions_without_candidates": positions_without_candidates,
        "positions_without_search": positions_without_search,
        "positions_without_intelligence": positions_without_intelligence,
        "zero_result_searches": zero_result_searches,
        "no_reply_searches": no_reply_searches,
        "privacy_hits": privacy_hits,
        "missing_notes": missing_notes,
    }


def project_label(client: Any, position: Any) -> str:
    c = clean(client)
    p = clean(position)
    if c and p:
        return f"{c}/{p}"
    if c:
        return f"{c}/未定岗位"
    if p:
        return f"未定客户/{p}"
    return "未定客户/未定岗位"


def candidate_label(row: sqlite3.Row) -> str:
    parts = [clean(row_get(row, "name")), clean(row_get(row, "candidate_name"))]
    name = next((part for part in parts if part), "未命名")
    company = clean(row_get(row, "company")) or clean(row_get(row, "candidate_company"))
    title = clean(row_get(row, "title")) or clean(row_get(row, "candidate_title"))
    suffix = " / ".join(part for part in [company, title] if part)
    return f"{name}（{suffix}）" if suffix else name


def render_report(audit: dict[str, Any]) -> str:
    counts = audit["counts"]
    missing = audit["missing_fields"]

    top_actions = []
    if audit["privacy_hits"]:
        top_actions.append(f"立即检查私密库敏感命中：{len(audit['privacy_hits'])} 个文件。")
    if audit["missing_notes"]:
        top_actions.append(f"重跑 Obsidian 私密主库同步：{len(audit['missing_notes'])} 名人选缺少候选人页。")
    if missing["intelligence"]:
        top_actions.append(f"优先补齐人岗匹配与下一步动作：{len(missing['intelligence'])} 名人选没有 intelligence 记录。")
    if audit["unmatched_tasks"]:
        top_actions.append(f"先处理待办归属：{len(audit['unmatched_tasks'])} 个打开待办仍是未匹配或低置信。")
    if audit["replies_without_open_task"]:
        top_actions.append(f"把有行动价值的回复补成待办：{len(audit['replies_without_open_task'])} 条回复没有打开待办。")
    if audit["positions_without_profile"]:
        top_actions.append(f"补岗位画像：{len(audit['positions_without_profile'])} 个在招岗位缺少岗位画像。")
    if not top_actions:
        top_actions.append("主链路没有明显断点，可以进入 Obsidian 优先读写试点。")

    status_rows = [[key, value] for key, value in audit["status_counter"].most_common()]
    reply_rows = [[key, value] for key, value in audit["reply_intent_counter"].most_common()]
    task_rows = [[key, value] for key, value in audit["task_status_counter"].most_common()]
    lane_rows = [[key, value] for key, value in audit["task_lane_counter"].most_common()]
    search_rows = [[key, value] for key, value in audit["search_status_counter"].most_common()]

    missing_rows = [
        ["缺客户归属", len(missing["client"])],
        ["缺岗位归属", len(missing["position"])],
        ["缺公司", len(missing["company"])],
        ["缺职位", len(missing["title"])],
        ["缺学历", len(missing["education"])],
        ["缺城市", len(missing["city"])],
        ["缺候选人画像", len(missing["profile"])],
        ["缺人岗匹配", len(missing["intelligence"])],
    ]

    duplicate_rows = []
    for (name, company), group in sorted(audit["duplicate_name_company"].items(), key=lambda item: len(item[1]), reverse=True)[:15]:
        projects = "；".join(project_label(row_get(row, "client"), row_get(row, "position")) for row in group[:4])
        duplicate_rows.append([name, company, len(group), projects])

    unmatched_task_rows = [
        [
            row_get(row, "id"),
            candidate_label(row),
            project_label(row_get(row, "client") or row_get(row, "inferred_client"), row_get(row, "position") or row_get(row, "inferred_position")),
            row_get(row, "match_confidence"),
            row_get(row, "task_type"),
            row_get(row, "lane_tag"),
        ]
        for row in audit["unmatched_tasks"][:20]
    ]

    reply_gap_rows = [
        [
            row_get(row, "id"),
            candidate_label(row),
            project_label(row_get(row, "client") or row_get(row, "inferred_client"), row_get(row, "position") or row_get(row, "inferred_position")),
            row_get(row, "intent"),
            row_get(row, "sentiment"),
            row_get(row, "suggested_next_action"),
        ]
        for row in audit["replies_without_open_task"][:20]
    ]

    touched_gap_rows = [
        [
            row_get(row, "id"),
            candidate_label(row),
            project_label(row_get(row, "client"), row_get(row, "position")),
            row_get(row, "status"),
            row_get(row, "updated_at"),
        ]
        for row in audit["candidates_touched_no_reply_no_task"][:20]
    ]

    role_gap_rows = [
        [client, position]
        for client, position in audit["positions_without_profile"][:30]
    ]

    search_gap_rows = [
        [
            row_get(row, "id"),
            project_label(row_get(row, "client"), row_get(row, "position")),
            row_get(row, "query"),
            row_get(row, "extracted_count"),
            row_get(row, "reply_count"),
            row_get(row, "status"),
        ]
        for row in audit["no_reply_searches"][:20]
    ]

    return "\n".join(
        [
            f"生成时间：{audit['generated_at']}",
            "",
            f"## 总体判断",
            "",
            f"- 主库健康分：{audit['score']}/100",
            f"- 阻断级问题：{audit['critical']}",
            f"- 待修问题量级：{audit['warnings']}",
            f"- 当前判断：{'可以继续运营，但需要先补齐人岗匹配和待办归属。' if audit['score'] < 80 else '主库可支撑日常运营，建议进入试点闭环。'}",
            "",
            "## 下一步优先级",
            "",
            bullet(top_actions),
            "",
            "## 数据规模",
            "",
            md_table(
                ["数据项", "数量"],
                [
                    ["候选人", counts["candidates"]],
                    ["候选人画像", counts["candidate_profiles"]],
                    ["人岗匹配", counts["candidate_intelligence"]],
                    ["回复", counts["candidate_replies"]],
                    ["打开/历史待办", counts["followup_tasks"]],
                    ["触达", counts["outreach_events"]],
                    ["搜索实验", counts["search_experiments"]],
                    ["客户反馈", counts["client_feedback_events"]],
                    ["岗位", counts["positions"]],
                    ["岗位画像", counts["position_profiles"]],
                ],
            ),
            "",
            "## 人选状态分布",
            "",
            md_table(["状态", "人数"], status_rows),
            "",
            "## 字段完整度",
            "",
            md_table(["问题", "数量"], missing_rows),
            "",
            "## 待办与回复",
            "",
            md_table(["待办状态", "数量"], task_rows),
            "",
            "### 打开待办分层",
            "",
            md_table(["分层", "数量"], lane_rows),
            "",
            "### 回复意图分布",
            "",
            md_table(["意图", "数量"], reply_rows),
            "",
            "### 待修待办样本",
            "",
            md_table(["待办ID", "人选", "项目", "匹配置信", "类型", "分层"], unmatched_task_rows) if unmatched_task_rows else "- 暂无",
            "",
            "### 有行动价值但没有打开待办的回复样本",
            "",
            md_table(["回复ID", "人选", "项目", "意图", "情绪", "建议动作"], reply_gap_rows) if reply_gap_rows else "- 暂无",
            "",
            "### 已触达但无回复无待办的人选样本",
            "",
            md_table(["人选ID", "人选", "项目", "状态", "更新时间"], touched_gap_rows) if touched_gap_rows else "- 暂无",
            "",
            "## 岗位健康",
            "",
            f"- 缺岗位画像：{len(audit['positions_without_profile'])}",
            f"- 岗位下没有候选人：{len(audit['positions_without_candidates'])}",
            f"- 岗位没有搜索实验：{len(audit['positions_without_search'])}",
            f"- 岗位没有人岗匹配记录：{len(audit['positions_without_intelligence'])}",
            "",
            "### 缺岗位画像样本",
            "",
            md_table(["客户", "岗位"], role_gap_rows) if role_gap_rows else "- 暂无",
            "",
            "## 搜索实验",
            "",
            md_table(["状态", "数量"], search_rows),
            "",
            f"- 零结果搜索：{len(audit['zero_result_searches'])}",
            f"- 有抽取但无回复搜索：{len(audit['no_reply_searches'])}",
            "",
            "### 有抽取但无回复的搜索样本",
            "",
            md_table(["实验ID", "项目", "关键词", "抽取", "回复", "状态"], search_gap_rows) if search_gap_rows else "- 暂无",
            "",
            "## 重复风险",
            "",
            f"- 同名同公司跨项目/岗位重复组：{len(audit['duplicate_name_company'])}",
            f"- 同名重复组：{len(audit['duplicate_name'])}",
            "",
            md_table(["姓名", "公司", "记录数", "项目样本"], duplicate_rows) if duplicate_rows else "- 暂无",
            "",
            "## Obsidian 同步与隐私边界",
            "",
            f"- SQLite 候选人缺 Obsidian 人选页：{len(audit['missing_notes'])}",
            f"- 敏感模式命中文件：{len(audit['privacy_hits'])}",
            "",
            "### 敏感命中文件",
            "",
            bullet(audit["privacy_hits"][:20]) if audit["privacy_hits"] else "- 未发现真实 token、Cookie、猎聘链接参数等敏感命中。",
            "",
            "## 建议推进节奏",
            "",
            "1. 先处理 `unmatched/low` 打开待办，把它们确认到客户和岗位。",
            "2. 对缺人岗匹配的人选批量重算 intelligence，让每个人都有推荐等级、风险点、验证问题和下一步动作。",
            "3. 对缺岗位画像的岗位补齐画像，再跑下一轮搜索策略。",
            "4. 把有行动价值但没有待办的回复补成待办，保证回复驾驶舱不会漏人。",
            "5. 选健康度最高的 2-3 个岗位做闭环试点，再进入 Obsidian 优先读写阶段。",
        ]
    )


def write_outputs(report_body: str, output_dir: Path, private_vault: Path, dry_run: bool) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"猎聘私密主库数据质量体检_{stamp}.md"
    private_path = private_vault / "60_Reviews" / "数据质量体检.md"
    full_report = "# 猎聘私密主库数据质量体检\n\n" + report_body.rstrip() + "\n"
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path.write_text(full_report, encoding="utf-8")
    replace_auto_block(private_path, "数据质量体检", report_body, dry_run=dry_run)
    return output_path, private_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Liepin private Obsidian vault data quality.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--private-vault", default=str(DEFAULT_PRIVATE_VAULT))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    private_vault = Path(args.private_vault).expanduser()
    output_dir = Path(args.output_dir).expanduser()

    conn = connect(db_path)
    audit = build_audit(conn, private_vault)
    report_body = render_report(audit)
    output_path, private_path = write_outputs(report_body, output_dir, private_vault, dry_run=args.dry_run)

    print(
        json.dumps(
            {
                "ok": True,
                "dry_run": args.dry_run,
                "score": audit["score"],
                "critical": audit["critical"],
                "warnings": audit["warnings"],
                "report": str(output_path),
                "private_note": str(private_path),
                "counts": audit["counts"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
