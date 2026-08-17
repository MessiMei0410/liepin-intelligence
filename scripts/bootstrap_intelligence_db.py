#!/usr/bin/env python3
"""Bootstrap the headhunting intelligence layer for the local talent pool."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from position_storage import ensure_position_storage_schema, seed_position_snapshots_from_positions

from a_system_agent.filter_models import MODEL_TABLE_DDL


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"
DEFAULT_BACKUP_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "backups"


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS candidate_intelligence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id INTEGER,
        candidate_name TEXT NOT NULL,
        candidate_company TEXT,
        client TEXT,
        position TEXT,
        fit_score INTEGER DEFAULT 0,
        fit_level TEXT DEFAULT 'unrated',
        evidence_json TEXT DEFAULT '{}',
        risk_json TEXT DEFAULT '{}',
        strong_matches_json TEXT DEFAULT '[]',
        weak_matches_json TEXT DEFAULT '[]',
        verification_questions_json TEXT DEFAULT '[]',
        recommendation_decision TEXT,
        next_action TEXT,
        last_evaluated_at TEXT,
        model_version TEXT DEFAULT 'rules-v0',
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(candidate_name, candidate_company, client, position)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS outreach_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id INTEGER,
        candidate_name TEXT NOT NULL,
        candidate_company TEXT,
        client TEXT,
        position TEXT,
        channel TEXT DEFAULT 'liepin',
        event_type TEXT NOT NULL,
        event_status TEXT DEFAULT 'done',
        message_summary TEXT,
        source_url TEXT,
        event_time TEXT DEFAULT (datetime('now','localtime')),
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate_replies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id INTEGER,
        candidate_name TEXT,
        candidate_company TEXT,
        client TEXT,
        position TEXT,
        channel TEXT DEFAULT 'liepin',
        conversation_id TEXT,
        message_time TEXT,
        direction TEXT DEFAULT 'candidate',
        raw_text TEXT NOT NULL,
        intent TEXT DEFAULT 'unclear',
        sentiment TEXT DEFAULT 'neutral',
        blockers_json TEXT DEFAULT '[]',
        suggested_next_action TEXT,
        processed_at TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(channel, conversation_id, message_time, raw_text)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS followup_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id INTEGER,
        candidate_name TEXT,
        candidate_company TEXT,
        client TEXT,
        position TEXT,
        task_type TEXT NOT NULL,
        priority INTEGER DEFAULT 2,
        due_at TEXT,
        status TEXT DEFAULT 'open',
        reason TEXT,
        resolution_note TEXT,
        closed_at TEXT,
        source_table TEXT,
        source_id INTEGER,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS search_experiments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client TEXT,
        position TEXT,
        channel TEXT DEFAULT 'liepin',
        round_name TEXT,
        query TEXT NOT NULL,
        filters_json TEXT DEFAULT '{}',
        result_count INTEGER,
        viewed_count INTEGER,
        extracted_count INTEGER,
        recommended_count INTEGER DEFAULT 0,
        reply_count INTEGER DEFAULT 0,
        positive_reply_count INTEGER DEFAULT 0,
        status TEXT DEFAULT 'open',
        source_url TEXT,
        noise_notes TEXT,
        run_time TEXT DEFAULT (datetime('now','localtime')),
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS client_feedback_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id INTEGER,
        candidate_name TEXT,
        candidate_company TEXT,
        client TEXT,
        position TEXT,
        feedback_type TEXT NOT NULL,
        status_after TEXT,
        reason_tags_json TEXT DEFAULT '[]',
        feedback_detail TEXT,
        next_action TEXT,
        source TEXT DEFAULT 'manual',
        feedback_time TEXT DEFAULT (datetime('now','localtime')),
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS position_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client TEXT NOT NULL,
        position TEXT NOT NULL,
        education_requirement TEXT,
        experience_requirement TEXT,
        hard_requirements_json TEXT DEFAULT '[]',
        ability_keywords_json TEXT DEFAULT '[]',
        target_companies_json TEXT DEFAULT '[]',
        exclusion_tags_json TEXT DEFAULT '[]',
        search_keywords_json TEXT DEFAULT '[]',
        soft_preferences_json TEXT DEFAULT '[]',
        pitch_points_json TEXT DEFAULT '[]',
        risk_points_json TEXT DEFAULT '[]',
        jd_analysis_summary TEXT,
        source_position_ids_json TEXT DEFAULT '[]',
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(client, position)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id INTEGER NOT NULL UNIQUE,
        candidate_name TEXT NOT NULL,
        candidate_company TEXT,
        client TEXT,
        position TEXT,
        education_level TEXT,
        seniority TEXT,
        industry_tags_json TEXT DEFAULT '[]',
        function_tags_json TEXT DEFAULT '[]',
        risk_tags_json TEXT DEFAULT '[]',
        profile_summary TEXT,
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_corrections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client TEXT,
        position TEXT,
        promote_keywords_json TEXT DEFAULT '[]',
        suppress_keywords_json TEXT DEFAULT '[]',
        target_tags_json TEXT DEFAULT '[]',
        blocker_tags_json TEXT DEFAULT '[]',
        evidence_json TEXT DEFAULT '[]',
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(client, position)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS position_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client TEXT NOT NULL,
        position TEXT NOT NULL,
        position_id INTEGER,
        source_type TEXT NOT NULL DEFAULT 'manual',
        source_ref TEXT,
        source_url TEXT,
        source_title TEXT,
        raw_text TEXT,
        raw_json TEXT DEFAULT '{}',
        content_hash TEXT NOT NULL,
        captured_at TEXT DEFAULT (datetime('now','localtime')),
        created_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(content_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS position_assets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client TEXT NOT NULL,
        position TEXT NOT NULL,
        asset_type TEXT NOT NULL,
        asset_title TEXT,
        asset_summary TEXT,
        file_path TEXT NOT NULL,
        source_snapshot_id INTEGER,
        asset_json TEXT DEFAULT '{}',
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(client, position, asset_type, file_path)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS learning_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client TEXT,
        position TEXT,
        topic TEXT NOT NULL,
        note TEXT NOT NULL,
        source TEXT,
        confidence INTEGER DEFAULT 3,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )
    """,
    """
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
    """,
    "CREATE INDEX IF NOT EXISTS idx_ci_client_position ON candidate_intelligence(client, position)",
    "CREATE INDEX IF NOT EXISTS idx_replies_client_position ON candidate_replies(client, position)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_status_due ON followup_tasks(status, due_at)",
    "CREATE INDEX IF NOT EXISTS idx_outreach_client_position ON outreach_events(client, position)",
    "CREATE INDEX IF NOT EXISTS idx_search_experiments_client_position ON search_experiments(client, position)",
    "CREATE INDEX IF NOT EXISTS idx_client_feedback_project ON client_feedback_events(client, position)",
    "CREATE INDEX IF NOT EXISTS idx_client_feedback_candidate ON client_feedback_events(candidate_name, candidate_company)",
    "CREATE INDEX IF NOT EXISTS idx_position_profiles_project ON position_profiles(client, position)",
    "CREATE INDEX IF NOT EXISTS idx_candidate_profiles_project ON candidate_profiles(client, position)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_corrections_project ON strategy_corrections(client, position)",
    "CREATE INDEX IF NOT EXISTS idx_reply_learning_rules_category ON reply_learning_rules(category)",
    "CREATE INDEX IF NOT EXISTS idx_position_snapshots_project_time ON position_snapshots(client, position, captured_at)",
    "CREATE INDEX IF NOT EXISTS idx_position_snapshots_source ON position_snapshots(source_type, source_ref)",
    "CREATE INDEX IF NOT EXISTS idx_position_assets_project_type ON position_assets(client, position, asset_type)",
]


SEARCH_EXPERIMENT_EXTRA_COLUMNS = {
    "round_name": "TEXT",
    "viewed_count": "INTEGER",
    "status": "TEXT DEFAULT 'open'",
    "source_url": "TEXT",
    "updated_at": "TEXT",
}

FOLLOWUP_TASK_EXTRA_COLUMNS = {
    "resolution_note": "TEXT",
    "closed_at": "TEXT",
}

CANDIDATE_REPLY_EXTRA_COLUMNS = {
    "reply_tags_json": "TEXT DEFAULT '[]'",
    "classification_reason": "TEXT",
    "classifier_version": "TEXT",
}

CANDIDATE_INTELLIGENCE_EXTRA_COLUMNS = {
    "strong_matches_json": "TEXT DEFAULT '[]'",
    "weak_matches_json": "TEXT DEFAULT '[]'",
    "verification_questions_json": "TEXT DEFAULT '[]'",
    "recommendation_decision": "TEXT",
}

POSITION_PROFILE_EXTRA_COLUMNS = {
    "soft_preferences_json": "TEXT DEFAULT '[]'",
    "pitch_points_json": "TEXT DEFAULT '[]'",
    "risk_points_json": "TEXT DEFAULT '[]'",
    "jd_analysis_summary": "TEXT",
}


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def backup_db(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"talent_pool_before_intelligence_{stamp}.db"
    shutil.copy2(db_path, backup_path)
    return backup_path


def ensure_schema(conn: sqlite3.Connection) -> list[str]:
    created = []
    existing = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for stmt in SCHEMA_STATEMENTS:
        conn.execute(stmt)
    conn.execute(MODEL_TABLE_DDL)
    search_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(search_experiments)")
    }
    for column, definition in SEARCH_EXPERIMENT_EXTRA_COLUMNS.items():
        if column not in search_columns:
            conn.execute(f"ALTER TABLE search_experiments ADD COLUMN {column} {definition}")
    task_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(followup_tasks)")
    }
    for column, definition in FOLLOWUP_TASK_EXTRA_COLUMNS.items():
        if column not in task_columns:
            conn.execute(f"ALTER TABLE followup_tasks ADD COLUMN {column} {definition}")
    reply_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(candidate_replies)")
    }
    for column, definition in CANDIDATE_REPLY_EXTRA_COLUMNS.items():
        if column not in reply_columns:
            conn.execute(f"ALTER TABLE candidate_replies ADD COLUMN {column} {definition}")
    intelligence_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(candidate_intelligence)")
    }
    for column, definition in CANDIDATE_INTELLIGENCE_EXTRA_COLUMNS.items():
        if column not in intelligence_columns:
            conn.execute(f"ALTER TABLE candidate_intelligence ADD COLUMN {column} {definition}")
    position_profile_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(position_profiles)")
    }
    for column, definition in POSITION_PROFILE_EXTRA_COLUMNS.items():
        if column not in position_profile_columns:
            conn.execute(f"ALTER TABLE position_profiles ADD COLUMN {column} {definition}")
    conn.commit()
    after = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for name in sorted(after - existing):
        if name in {
            "candidate_intelligence",
            "candidate_replies",
            "client_feedback_events",
            "position_profiles",
            "candidate_profiles",
        "strategy_corrections",
        "position_snapshots",
        "position_assets",
        "followup_tasks",
        "learning_notes",
        "reply_learning_rules",
        "outreach_events",
            "search_experiments",
        }:
            created.append(name)
    return created


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    if not row:
        return 0
    value = row[0]
    return int(value or 0)


def fetch_all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def classify_position_priority(row: dict) -> int:
    open_gap = int(row.get("gap") or 0)
    candidate_count = int(row.get("candidate_count") or 0)
    recommended_count = int(row.get("recommended_count") or 0)
    contacted_count = int(row.get("contacted_count") or 0)
    score = 0
    if open_gap > 0:
        score += 40
    if candidate_count >= 10:
        score += 25
    if recommended_count or contacted_count:
        score += 20
    if candidate_count and recommended_count == 0:
        score += 10
    if candidate_count > 80:
        score -= 10
    return score


def build_report(conn: sqlite3.Connection, created_tables: list[str], backup_path: Path) -> dict:
    report: dict = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "db_path": str(DEFAULT_DB),
        "backup_path": str(backup_path),
        "created_tables": created_tables,
        "totals": {},
        "client_status": [],
        "top_positions": [],
        "data_quality": {},
        "recommended_next_steps": [],
    }

    report["totals"]["candidates"] = scalar(conn, "SELECT COUNT(*) FROM candidates") if table_exists(conn, "candidates") else 0
    report["totals"]["positions"] = scalar(conn, "SELECT COUNT(*) FROM positions") if table_exists(conn, "positions") else 0
    for table in [
        "candidate_intelligence",
        "outreach_events",
        "candidate_replies",
        "followup_tasks",
        "search_experiments",
        "client_feedback_events",
        "position_profiles",
        "candidate_profiles",
        "strategy_corrections",
        "position_snapshots",
        "position_assets",
        "learning_notes",
        "reply_learning_rules",
    ]:
        report["totals"][table] = scalar(conn, f"SELECT COUNT(*) FROM {table}")

    if table_exists(conn, "candidates"):
        report["client_status"] = fetch_all(
            conn,
            """
            SELECT
                COALESCE(NULLIF(client, ''), '未标客户') AS client,
                COALESCE(NULLIF(status, ''), '未标状态') AS status,
                COUNT(*) AS count
            FROM candidates
            GROUP BY client, status
            ORDER BY count DESC, client, status
            LIMIT 80
            """,
        )
        report["data_quality"] = {
            "missing_client": scalar(conn, "SELECT COUNT(*) FROM candidates WHERE client IS NULL OR client=''"),
            "missing_position": scalar(conn, "SELECT COUNT(*) FROM candidates WHERE position IS NULL OR position=''"),
            "missing_company": scalar(conn, "SELECT COUNT(*) FROM candidates WHERE company IS NULL OR company=''"),
            "missing_title": scalar(conn, "SELECT COUNT(*) FROM candidates WHERE title IS NULL OR title=''"),
            "only_new_status": scalar(conn, "SELECT COUNT(*) FROM candidates WHERE status='new'"),
        }
        report["top_positions"] = fetch_all(
            conn,
            """
            WITH c AS (
              SELECT
                client,
                position,
                COUNT(*) AS candidate_count,
                SUM(CASE WHEN status='recommended' THEN 1 ELSE 0 END) AS recommended_count,
                SUM(CASE WHEN status='contacted' THEN 1 ELSE 0 END) AS contacted_count,
                SUM(CASE WHEN status IN ('client_approved','interviewing','offered','hired') THEN 1 ELSE 0 END) AS advanced_count
              FROM candidates
              WHERE client IS NOT NULL AND client != ''
              GROUP BY client, position
            )
            SELECT
              c.client,
              COALESCE(NULLIF(c.position,''), '未标岗位') AS position,
              c.candidate_count,
              c.recommended_count,
              c.contacted_count,
              c.advanced_count,
              COALESCE(p.gap, 0) AS gap,
              COALESCE(p.status, '') AS position_status
            FROM c
            LEFT JOIN positions p
              ON p.client = c.client
             AND (p.title = c.position OR c.position LIKE '%' || p.title || '%' OR p.title LIKE '%' || c.position || '%')
            ORDER BY c.candidate_count DESC
            LIMIT 40
            """,
        )
        for row in report["top_positions"]:
            row["intelligence_priority"] = classify_position_priority(row)
        report["top_positions"].sort(key=lambda item: item["intelligence_priority"], reverse=True)

    if report["data_quality"].get("missing_position", 0) > 0:
        report["recommended_next_steps"].append("先补候选人岗位字段，否则回复和推荐无法稳定归因到具体岗位。")
    if report["totals"].get("candidate_replies", 0) == 0:
        report["recommended_next_steps"].append("把正在跑的职聊回复抓取接入 candidate_replies 表，并自动生成 followup_tasks。")
    if report["totals"].get("candidate_intelligence", 0) == 0:
        report["recommended_next_steps"].append("对重点岗位先跑候选人匹配评分，填充 candidate_intelligence。")
    if report["totals"].get("search_experiments", 0) == 0:
        report["recommended_next_steps"].append("后续每轮猎聘搜索都记录 query、筛选条件、结果数和转化结果。")
    if report["totals"].get("position_snapshots", 0) == 0:
        report["recommended_next_steps"].append("先把 positions 表里的现有岗位快照入库到 position_snapshots，作为历史证据底座。")
    if report["totals"].get("position_assets", 0) == 0:
        report["recommended_next_steps"].append("把岗位详情页、推进入口和摘要文件登记进 position_assets，方便人和 agent 同步读取。")

    return report


def write_markdown(report: dict, output_path: Path) -> None:
    lines = [
        "# 猎聘智能底座巡检报告",
        "",
        f"生成时间：{report['generated_at']}",
        f"数据库：`{report['db_path']}`",
        f"备份：`{report['backup_path']}`",
        "",
        "## 一、结构变更",
        "",
    ]
    if report["created_tables"]:
        lines.append("本次新建表：")
        for table in report["created_tables"]:
            lines.append(f"- `{table}`")
    else:
        lines.append("智能表结构已存在，本次未重复新建。")
    lines.extend(["", "## 二、总体数量", ""])
    for key, value in report["totals"].items():
        lines.append(f"- {key}: {value}")

    lines.extend(["", "## 三、数据质量", ""])
    labels = {
        "missing_client": "缺客户",
        "missing_position": "缺岗位",
        "missing_company": "缺公司",
        "missing_title": "缺职位",
        "only_new_status": "仍停留 new 状态",
    }
    for key, label in labels.items():
        lines.append(f"- {label}: {report['data_quality'].get(key, 0)}")

    lines.extend(["", "## 四、优先智能化岗位", ""])
    if report["top_positions"]:
        lines.append("| 优先级 | 客户 | 岗位 | 候选人 | 已推荐 | 已沟通 | 进阶 | 缺口 |")
        lines.append("|---:|---|---|---:|---:|---:|---:|---:|")
        for row in report["top_positions"][:20]:
            lines.append(
                "| {priority} | {client} | {position} | {candidate_count} | {recommended_count} | {contacted_count} | {advanced_count} | {gap} |".format(
                    priority=row.get("intelligence_priority", 0),
                    client=row.get("client", ""),
                    position=row.get("position", ""),
                    candidate_count=row.get("candidate_count", 0),
                    recommended_count=row.get("recommended_count", 0),
                    contacted_count=row.get("contacted_count", 0),
                    advanced_count=row.get("advanced_count", 0),
                    gap=row.get("gap", 0),
                )
            )
    else:
        lines.append("暂无可统计岗位。")

    lines.extend(["", "## 五、状态分布", ""])
    if report["client_status"]:
        lines.append("| 客户 | 状态 | 数量 |")
        lines.append("|---|---|---:|")
        for row in report["client_status"][:40]:
            lines.append(f"| {row['client']} | {row['status']} | {row['count']} |")
    else:
        lines.append("暂无候选人状态数据。")

    lines.extend(["", "## 六、下一步", ""])
    for item in report["recommended_next_steps"]:
        lines.append(f"- {item}")

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize intelligence tables for the local headhunting talent pool.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    backup_dir = Path(args.backup_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    backup_path = backup_db(db_path, backup_dir)
    conn = connect(db_path)
    try:
        created_tables = ensure_schema(conn)
        if table_exists(conn, "positions"):
            seed_position_snapshots_from_positions(conn)
        ensure_position_storage_schema(conn)
        report = build_report(conn, created_tables, backup_path)
    finally:
        conn.close()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"intelligence_bootstrap_report_{stamp}.json"
    md_path = output_dir / f"猎聘智能底座巡检报告_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, md_path)

    print(json.dumps({"ok": True, "backup": str(backup_path), "json": str(json_path), "markdown": str(md_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
