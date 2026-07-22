#!/usr/bin/env python3
"""Standardize position and candidate profiles for the Liepin workflow."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from position_storage import (
    clean as storage_clean,
    ensure_position_storage_schema,
    fetch_latest_position_snapshot,
    fetch_position_snapshots,
    table_exists,
    upsert_position_asset,
)


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


POSITION_SCHEMA = """
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
"""


POSITION_EXTRA_COLUMNS = {
    "soft_preferences_json": "TEXT DEFAULT '[]'",
    "pitch_points_json": "TEXT DEFAULT '[]'",
    "risk_points_json": "TEXT DEFAULT '[]'",
    "jd_analysis_summary": "TEXT",
}


CANDIDATE_SCHEMA = """
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
"""


KEYWORD_RULES = {
    "PVD": ["PVD", "磁控溅射", "Endura", "Ta", "Cu", "TiN", "Chamber", "真空镀膜"],
    "CVD": ["CVD", "Metal CVD", "PECVD", "ALD", "薄膜沉积", "Hydra"],
    "机械": ["机械", "SolidWorks", "AutoCAD", "结构", "零件加工", "装配", "真空"],
    "电气/硬件": ["电气", "硬件", "电源", "电力电子", "FPGA", "射频", "控制"],
    "工艺": ["工艺", "DOE", "Profile", "Particle", "均匀性", "WPH", "COO"],
    "可靠性": ["可靠性", "环境试验", "验证测试", "失效分析", "统计"],
    "质量/PQE": ["PQE", "质量", "制程质量", "QA", "QE", "QRA", "良率", "MSA"],
    "系统": ["系统", "集成", "总体设计", "方案策划", "项目设计"],
    "材料": ["材料", "化学", "物理", "表面处理", "吸附", "污染颗粒"],
    "软件/CIM": ["CIM", "MES", "ERP", "SQL", "Java", "智能制造", "自动化"],
}

COMPANY_RULES = {
    "设备商": ["AMAT", "应用材料", "Lam", "泛林", "TEL", "东京电子", "北方华创", "中微", "拓荆", "微导纳米", "屹唐", "盛美"],
    "Fab/IDM": ["中芯", "长鑫", "华虹", "华力", "士兰", "台积电", "SK海力士", "海力士", "三星", "燕东", "荣芯"],
    "封测/模组": ["长电", "通富", "华天", "封装", "测试", "模组"],
}


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(POSITION_SCHEMA)
    conn.execute(CANDIDATE_SCHEMA)
    position_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(position_profiles)")
    }
    for column, definition in POSITION_EXTRA_COLUMNS.items():
        if column not in position_columns:
            conn.execute(f"ALTER TABLE position_profiles ADD COLUMN {column} {definition}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_position_profiles_project ON position_profiles(client, position)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_candidate_profiles_project ON candidate_profiles(client, position)")
    conn.commit()


def clean(value: Any) -> str:
    return storage_clean(value)


def dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys([clean(item) for item in items if clean(item)]))


def tags_from_text(text: str, rules: dict[str, list[str]]) -> list[str]:
    lower = text.lower()
    tags = []
    for tag, needles in rules.items():
        if any(needle.lower() in lower for needle in needles):
            tags.append(tag)
    return tags


def snippets_from_text(text: str, needles: list[str]) -> list[str]:
    lines = re.split(r"[。；;\n]+|(?=\d+[.．、])", text)
    snippets = []
    for line in lines:
        item = clean(line)
        if any(needle.lower() in item.lower() for needle in needles):
            snippets.append(item[:70])
    return dedupe(snippets)[:8]


def soft_preferences_from_text(text: str, ability_tags: list[str], target_tags: list[str]) -> list[str]:
    preferences: list[str] = []
    if target_tags:
        preferences.append(f"优先来自{'、'.join(target_tags[:2])}背景")
    if ability_tags:
        preferences.append(f"优先具备{'、'.join(ability_tags[:3])}相关经验")
    if any(key in text for key in ["英文", "英语", "海外", "外企"]):
        preferences.append("有英文沟通或外企/海外协作经验更佳")
    if any(key in text for key in ["管理", "带团队", "团队"]):
        preferences.append("有团队管理或跨部门推进经验更佳")
    if any(key in text for key in ["量产", "交付", "客户现场", "现场"]):
        preferences.append("有量产交付或客户现场问题闭环经验更佳")
    return dedupe(preferences)[:6]


def pitch_points_from_profile(client: str, position: str, ability_tags: list[str], target_tags: list[str]) -> list[str]:
    points = []
    if client:
        points.append(f"{client}在招方向明确，岗位匹配度高的人选可以快速进入沟通")
    if position:
        points.append(f"岗位聚焦{position}，适合希望继续深耕该方向的人选")
    if ability_tags:
        points.append(f"技术关键词清晰：{'、'.join(ability_tags[:4])}")
    if target_tags:
        points.append(f"目标背景明确：{'、'.join(target_tags[:3])}")
    return dedupe(points)[:6]


def risk_points_from_text(text: str, hard_requirements: list[str], exclusion_tags: list[str]) -> list[str]:
    risks = []
    if not hard_requirements:
        risks.append("JD硬性条件不完整，推荐前需补确认")
    if not exclusion_tags:
        risks.append("排除项未明确，容易出现客户口径后置")
    if not any(key in text for key in ["薪", "年薪", "月薪", "待遇"]):
        risks.append("薪资信息缺失，触达前需确认可谈区间")
    if not any(key in text for key in ["城市", "地点", "上海", "北京", "苏州", "无锡", "深圳", "杭州"]):
        risks.append("工作地点不清，候选人意愿判断会受影响")
    if len(text) < 80:
        risks.append("JD信息偏短，岗位卖点和筛选口径需要人工补充")
    return dedupe(risks)[:6]


def jd_summary(
    client: str,
    position: str,
    hard_requirements: list[str],
    ability_tags: list[str],
    target_tags: list[str],
    risks: list[str],
) -> str:
    parts = [f"{client}/{position}"]
    if hard_requirements:
        parts.append(f"硬性门槛：{'、'.join(hard_requirements[:3])}")
    if ability_tags:
        parts.append(f"能力方向：{'、'.join(ability_tags[:4])}")
    if target_tags:
        parts.append(f"目标背景：{'、'.join(target_tags[:3])}")
    if risks:
        parts.append(f"待补：{'、'.join(risks[:2])}")
    return "；".join(parts)


def education_from_text(*parts: str) -> str:
    text = " ".join(parts)
    if "博士" in text:
        return "博士"
    if "硕士" in text or "研究生" in text:
        return "硕士"
    if "本科" in text:
        return "本科"
    if "大专" in text or "专科" in text:
        return "大专"
    return ""


def experience_from_text(*parts: str) -> str:
    text = " ".join(parts)
    matches = re.findall(r"(\d+)\s*年以上", text)
    if matches:
        return f"{max(int(item) for item in matches)}年以上"
    matches = re.findall(r"(\d+)\s*年", text)
    if matches:
        return f"{max(int(item) for item in matches)}年"
    return ""


def seniority_from_text(text: str) -> str:
    lower = text.lower()
    if any(key in lower for key in ["总监", "负责人", "director"]):
        return "总监/负责人"
    if any(key in lower for key in ["经理", "manager"]):
        return "经理"
    if any(key in lower for key in ["主管", "leader", "组长"]):
        return "主管/Leader"
    if any(key in lower for key in ["专家", "资深", "高级"]):
        return "专家/高级"
    if "工程师" in lower:
        return "工程师"
    return "未识别"


def load_positions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if table_exists(conn, "position_snapshots"):
        rows = conn.execute(
            """
            SELECT
                ps.client,
                ps.position AS title,
                p.level,
                p.education,
                p.experience,
                p.requirements,
                p.responsibilities,
                p.gap,
                p.status,
                p.id,
                p.created_at,
                p.updated_at
            FROM position_snapshots ps
            LEFT JOIN positions p
              ON p.id = ps.position_id
            ORDER BY datetime(COALESCE(ps.captured_at, ps.created_at)) DESC, ps.id DESC
            """
        ).fetchall()
        if rows:
            return rows
    return conn.execute(
        """
        SELECT id, client, title, level, education, experience, requirements, responsibilities, gap, status
        FROM positions
        WHERE COALESCE(status, 'open') = 'open'
        ORDER BY client, title, id
        """
    ).fetchall()


def load_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, name, company, title, education, experience, skills, city, client, position, status, notes
        FROM candidates
        ORDER BY updated_at DESC, id DESC
        """
    ).fetchall()


def build_position_profiles(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        key = (clean(row["client"]), clean(row["title"]))
        grouped.setdefault(key, []).append(row)

    profiles = []
    for (client, position), group in grouped.items():
        text = " ".join(
            clean(row["title"]) + " " + clean(row["level"]) + " " + clean(row["education"]) + " "
            + clean(row["experience"]) + " " + clean(row["requirements"]) + " " + clean(row["responsibilities"])
            for row in group
        )
        ability_tags = tags_from_text(text, KEYWORD_RULES)
        target_tags = tags_from_text(text, COMPANY_RULES)
        search_keywords = dedupe([position, *ability_tags, *target_tags])
        hard_requirements = dedupe(
            [
                education_from_text(text),
                experience_from_text(text),
                *snippets_from_text(text, ["学历", "年以上", "熟悉", "具备", "经验"]),
            ]
        )[:10]
        exclusion_tags = []
        if "博士" in text:
            exclusion_tags.append("学历不足")
        if "10年以上" in text:
            exclusion_tags.append("年限不足")
        if any(key in text for key in ["PVD", "CVD", "ALD", "PECVD"]):
            exclusion_tags.append("缺半导体设备经验")
        exclusion_tags = dedupe(exclusion_tags)
        soft_preferences = soft_preferences_from_text(text, ability_tags, target_tags)
        pitch_points = pitch_points_from_profile(client, position, ability_tags, target_tags)
        risk_points = risk_points_from_text(text, hard_requirements, exclusion_tags)
        profiles.append(
            {
                "client": client,
                "position": position,
                "education_requirement": education_from_text(text),
                "experience_requirement": experience_from_text(text),
                "hard_requirements_json": json.dumps(hard_requirements, ensure_ascii=False),
                "ability_keywords_json": json.dumps(ability_tags, ensure_ascii=False),
                "target_companies_json": json.dumps(target_tags, ensure_ascii=False),
                "exclusion_tags_json": json.dumps(exclusion_tags, ensure_ascii=False),
                "search_keywords_json": json.dumps(search_keywords, ensure_ascii=False),
                "soft_preferences_json": json.dumps(soft_preferences, ensure_ascii=False),
                "pitch_points_json": json.dumps(pitch_points, ensure_ascii=False),
                "risk_points_json": json.dumps(risk_points, ensure_ascii=False),
                "jd_analysis_summary": jd_summary(
                    client, position, hard_requirements, ability_tags, target_tags, risk_points
                ),
                "source_position_ids_json": json.dumps([int(row["id"]) for row in group], ensure_ascii=False),
            }
        )
    return profiles


def build_candidate_profile(row: sqlite3.Row) -> dict[str, Any]:
    text = " ".join(
        clean(row[key])
        for key in ["name", "company", "title", "education", "experience", "skills", "city", "client", "position", "notes"]
    )
    industry_tags = tags_from_text(text, COMPANY_RULES)
    function_tags = tags_from_text(text, KEYWORD_RULES)
    risks = []
    if not clean(row["company"]):
        risks.append("缺公司")
    if not clean(row["title"]):
        risks.append("缺职位")
    if not clean(row["education"]):
        risks.append("缺学历")
    if not clean(row["experience"]):
        risks.append("缺经历")
    if clean(row["status"]) in {"eliminated", "client_rejected"}:
        risks.append("已负向")
    summary_parts = [
        clean(row["company"]),
        clean(row["title"]),
        "、".join(function_tags[:3]),
        clean(row["city"]),
    ]
    return {
        "candidate_id": int(row["id"]),
        "candidate_name": clean(row["name"]) or "未识别",
        "candidate_company": clean(row["company"]),
        "client": clean(row["client"]),
        "position": clean(row["position"]),
        "education_level": education_from_text(clean(row["education"])),
        "seniority": seniority_from_text(text),
        "industry_tags_json": json.dumps(industry_tags, ensure_ascii=False),
        "function_tags_json": json.dumps(function_tags, ensure_ascii=False),
        "risk_tags_json": json.dumps(dedupe(risks), ensure_ascii=False),
        "profile_summary": "｜".join(item for item in summary_parts if item)[:220],
    }


def upsert_position_profiles(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> None:
    sql = """
    INSERT INTO position_profiles (
        client, position, education_requirement, experience_requirement,
        hard_requirements_json, ability_keywords_json, target_companies_json,
        exclusion_tags_json, search_keywords_json, soft_preferences_json,
        pitch_points_json, risk_points_json, jd_analysis_summary,
        source_position_ids_json, updated_at
    ) VALUES (
        :client, :position, :education_requirement, :experience_requirement,
        :hard_requirements_json, :ability_keywords_json, :target_companies_json,
        :exclusion_tags_json, :search_keywords_json, :soft_preferences_json,
        :pitch_points_json, :risk_points_json, :jd_analysis_summary,
        :source_position_ids_json, datetime('now','localtime')
    )
    ON CONFLICT(client, position) DO UPDATE SET
        education_requirement=excluded.education_requirement,
        experience_requirement=excluded.experience_requirement,
        hard_requirements_json=excluded.hard_requirements_json,
        ability_keywords_json=excluded.ability_keywords_json,
        target_companies_json=excluded.target_companies_json,
        exclusion_tags_json=excluded.exclusion_tags_json,
        search_keywords_json=excluded.search_keywords_json,
        soft_preferences_json=excluded.soft_preferences_json,
        pitch_points_json=excluded.pitch_points_json,
        risk_points_json=excluded.risk_points_json,
        jd_analysis_summary=excluded.jd_analysis_summary,
        source_position_ids_json=excluded.source_position_ids_json,
        updated_at=datetime('now','localtime')
    """
    conn.executemany(sql, records)
    conn.commit()


def upsert_candidate_profiles(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> None:
    sql = """
    INSERT INTO candidate_profiles (
        candidate_id, candidate_name, candidate_company, client, position,
        education_level, seniority, industry_tags_json, function_tags_json,
        risk_tags_json, profile_summary, updated_at
    ) VALUES (
        :candidate_id, :candidate_name, :candidate_company, :client, :position,
        :education_level, :seniority, :industry_tags_json, :function_tags_json,
        :risk_tags_json, :profile_summary, datetime('now','localtime')
    )
    ON CONFLICT(candidate_id) DO UPDATE SET
        candidate_name=excluded.candidate_name,
        candidate_company=excluded.candidate_company,
        client=excluded.client,
        position=excluded.position,
        education_level=excluded.education_level,
        seniority=excluded.seniority,
        industry_tags_json=excluded.industry_tags_json,
        function_tags_json=excluded.function_tags_json,
        risk_tags_json=excluded.risk_tags_json,
        profile_summary=excluded.profile_summary,
        updated_at=datetime('now','localtime')
    """
    conn.executemany(sql, records)
    conn.commit()


def write_report(output_dir: Path, positions: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘画像标准化_{stamp}.md"
    ability_counts = Counter(tag for row in positions for tag in json.loads(row["ability_keywords_json"]))
    candidate_function_counts = Counter(tag for row in candidates for tag in json.loads(row["function_tags_json"]))
    risk_counts = Counter(tag for row in candidates for tag in json.loads(row["risk_tags_json"]))

    lines = [
        "# 猎聘画像标准化",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 概览",
        "",
        f"- 岗位画像：{len(positions)} 个客户/岗位方向",
        f"- 候选人画像：{len(candidates)} 人",
        f"- 岗位能力标签：{'、'.join(f'{k} {v}' for k, v in ability_counts.most_common(10)) or '暂无'}",
        f"- 候选人能力标签：{'、'.join(f'{k} {v}' for k, v in candidate_function_counts.most_common(10)) or '暂无'}",
        f"- 候选人资料风险：{'、'.join(f'{k} {v}' for k, v in risk_counts.most_common(10)) or '暂无'}",
        "",
        "## 岗位画像样例",
        "",
        "| 项目 | 硬性门槛 | 软性偏好 | 目标公司/背景 | 搜索关键词 | 沟通卖点 | 风险/待补 |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in positions[:20]:
        lines.append(
            "| {project} | {hard} | {soft} | {target} | {search} | {pitch} | {risk} |".format(
                project=f"{row['client']}/{row['position']}".replace("|", "｜"),
                hard="、".join(json.loads(row["hard_requirements_json"])[:4]) or "未提",
                soft="、".join(json.loads(row["soft_preferences_json"])[:3]) or "未提",
                target="、".join(json.loads(row["target_companies_json"])[:4]) or "未识别",
                search="、".join(json.loads(row["search_keywords_json"])[:6]) or "未识别",
                pitch="、".join(json.loads(row["pitch_points_json"])[:2]) or "未生成",
                risk="、".join(json.loads(row["risk_points_json"])[:3]) or "无",
            )
        )
    lines.extend(
        [
            "",
            "## JD 分析摘要样例",
            "",
        ]
    )
    for row in positions[:10]:
        lines.append(f"- {row['jd_analysis_summary']}")
    lines.extend(
        [
            "",
            "## 候选人画像样例",
            "",
            "| 人选 | 项目 | 学历 | 层级 | 能力标签 | 风险 | 摘要 |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for row in candidates[:30]:
        lines.append(
            "| {name} | {project} | {edu} | {seniority} | {tags} | {risk} | {summary} |".format(
                name=row["candidate_name"].replace("|", "｜"),
                project=f"{row['client'] or '未定客户'}/{row['position'] or '未定岗位'}".replace("|", "｜"),
                edu=row["education_level"] or "未识别",
                seniority=row["seniority"],
                tags="、".join(json.loads(row["function_tags_json"])[:5]) or "未识别",
                risk="、".join(json.loads(row["risk_tags_json"])) or "无",
                summary=row["profile_summary"].replace("|", "｜"),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_asset_record(conn: sqlite3.Connection, client: str, position: str, report_path: Path, report_title: str, snapshot_id: int | None, summary: str) -> None:
    upsert_position_asset(
        conn,
        {
            "client": client,
            "position": position,
            "asset_type": "position_profile_report",
            "asset_title": report_title,
            "asset_summary": summary,
            "file_path": str(report_path),
            "source_snapshot_id": snapshot_id,
            "asset_json": json.dumps({"kind": "generated_report", "path": str(report_path)}, ensure_ascii=False),
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate standardized position and candidate profiles.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = connect(Path(args.db).expanduser())
    try:
        ensure_schema(conn)
        ensure_position_storage_schema(conn)
        position_records = build_position_profiles(load_positions(conn))
        candidate_records = [build_candidate_profile(row) for row in load_candidates(conn)]
        if not args.dry_run:
            upsert_position_profiles(conn, position_records)
            upsert_candidate_profiles(conn, candidate_records)
    finally:
        conn.close()

    report = write_report(Path(args.output_dir).expanduser(), position_records, candidate_records)
    conn = connect(Path(args.db).expanduser())
    try:
        ensure_position_storage_schema(conn)
        for record in position_records[:50]:
            snapshot = fetch_latest_position_snapshot(conn, record["client"], record["position"])
            write_asset_record(
                conn,
                record["client"],
                record["position"],
                report,
                f"{record['client']}/{record['position']} 画像标准化报告",
                int(snapshot["id"]) if snapshot else None,
                record["jd_analysis_summary"],
            )
    finally:
        conn.close()
    print(
        json.dumps(
            {
                "ok": True,
                "dry_run": args.dry_run,
                "position_profiles": len(position_records),
                "candidate_profiles": len(candidate_records),
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
