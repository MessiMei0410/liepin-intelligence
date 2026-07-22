#!/usr/bin/env python3
"""Generate the next-round Liepin search strategy from project signals."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from generate_position_dashboard import (
    DEFAULT_DB,
    DEFAULT_OUTPUT_DIR,
    NEGATIVE_FEEDBACK,
    POSITIVE_FEEDBACK,
    clean,
    collect_projects,
    connect,
    display_project,
    first_line,
    table_exists,
)


def safe_rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def split_project(project: dict[str, Any]) -> tuple[str, str]:
    return clean(project.get("client")), clean(project.get("position"))


def parse_json_list(text: str | None) -> list[str]:
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [clean(item) for item in value if clean(item)]


def dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys([item for item in items if item]))


def strategy_score(project: dict[str, Any]) -> float:
    score = 0.0
    score += min(int(project.get("gap") or 0), 5) * 13
    score += max(0, 4 - int(project.get("active_candidates") or 0)) * 11
    score += int(project.get("positive_replies") or 0) * 8
    score += int(project.get("positive_feedback") or 0) * 18
    score += int(project.get("negative_feedback") or 0) * 10
    score += int(project.get("hold_feedback") or 0) * 5
    if int(project.get("open_position_rows") or 0):
        score += 10
    if int(project.get("search_experiments") or 0) == 0:
        score += 8
    if int(project.get("search_viewed") or 0) and int(project.get("search_extracted") or 0) == 0:
        score += 8
    if int(project.get("search_experiments") or 0) and int(project.get("outreach_events") or 0):
        score += 9
    elif int(project.get("search_experiments") or 0) and int(project.get("advanced_candidates") or 0):
        score += 6
    if not project.get("client") or not project.get("position"):
        score -= 30
    return round(score, 1)


def project_reason(project: dict[str, Any]) -> str:
    reasons: list[str] = []
    if project.get("gap"):
        reasons.append(f"缺口 {project['gap']}")
    if int(project.get("active_candidates") or 0) < 3:
        reasons.append(f"池子偏薄，仅 {project.get('active_candidates') or 0} 人")
    if project.get("positive_feedback"):
        reasons.append(f"有 {project['positive_feedback']} 条客户正向反馈，可扩相似人选")
    if project.get("negative_feedback"):
        reasons.append(f"有 {project['negative_feedback']} 条客户负向反馈，需要修正方向")
    if project.get("positive_replies"):
        reasons.append(f"有 {project['positive_replies']} 条可继续回复")
    if not project.get("search_experiments"):
        reasons.append("还没有搜索实验记录")
    elif project.get("outreach_events"):
        reasons.append(f"已有 {project['search_experiments']} 轮搜索实验和 {project['outreach_events']} 条触达，适合看转化再扩搜")
    return "；".join(reasons) or "需要继续观察岗位转化"


def keyword_candidates(position: str, learned_keywords: list[str] | None = None) -> list[str]:
    lower = position.lower()
    keywords = list(learned_keywords or []) + [position]
    rules = [
        (("机械",), ["机械工程师", "机械设计", "设备机械", "半导体设备 机械"]),
        (("资深机械",), ["精密机械 设计 半导体", "运动台 机械设计", "精密运动 机械 半导体", "Ansys 机械 半导体"]),
        (("电源", "电力电子"), ["电源工程师", "电力电子", "射频电源", "半导体设备 电源"]),
        (("fpga",), ["FPGA", "数字电路", "硬件逻辑", "半导体设备 FPGA"]),
        (("硬件",), ["硬件工程师", "模拟电路", "数字电路", "半导体设备 硬件"]),
        (("pvd", "磁控", "溅射"), ["PVD", "磁控溅射", "薄膜设备", "真空镀膜"]),
        (("cvd",), ["CVD", "Metal CVD", "薄膜沉积", "半导体设备 CVD"]),
        (("工艺",), ["工艺工程师", "薄膜工艺", "半导体工艺", "PVD CVD 工艺"]),
        (("材料",), ["材料工程师", "薄膜材料", "半导体材料", "材料研发"]),
        (("可靠",), ["可靠性工程师", "质量可靠性", "失效分析", "半导体设备 可靠性"]),
        (("系统",), ["系统工程师", "设备系统", "系统集成", "半导体设备 系统"]),
        (("总监", "经理", "负责人"), ["技术负责人", "研发经理", "部门负责人", "半导体设备 管理"]),
    ]
    for needles, values in rules:
        if any(needle in lower for needle in needles):
            keywords.extend(values)
    return list(dict.fromkeys([item for item in keywords if item]))[:6]


def filter_suggestions(project: dict[str, Any]) -> list[str]:
    position = clean(project.get("position"))
    lower = position.lower()
    filters = ["行业先锁半导体设备/泛半导体设备，再按岗位关键词放宽"]
    if any(key in lower for key in ["总监", "经理", "负责人"]):
        filters.append("职级优先经理/总监/负责人，不要只搜工程师")
    else:
        filters.append("职级先搜工程师/高级工程师，再补专家和小团队负责人")
    if any(key in lower for key in ["pvd", "cvd", "磁控", "溅射", "工艺"]):
        filters.append("目标公司优先薄膜沉积/PVD/CVD/真空设备相关公司")
    if int(project.get("search_viewed") or 0) and int(project.get("search_extracted") or 0) == 0:
        filters.append("上一轮查看后无入库，下一轮加公司或设备类型限制降噪")
    if int(project.get("active_candidates") or 0) < 3:
        filters.append("先看最近活跃和可沟通人选，目标是补到 5-8 个可聊对象")
    if int(project.get("search_experiments") or 0) and int(project.get("outreach_events") or 0):
        filters.append("先等已打招呼人选回复；补搜时只扩相近关键词，不重复扫泛词")
    return filters[:4]


def load_strategy_corrections(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, list[str]]]:
    corrections: dict[tuple[str, str], dict[str, list[str]]] = {}
    if not table_exists(conn, "strategy_corrections"):
        return corrections
    for row in safe_rows(
        conn,
        """
        SELECT client, position, promote_keywords_json, suppress_keywords_json,
               target_tags_json, blocker_tags_json, evidence_json
        FROM strategy_corrections
        ORDER BY datetime(updated_at) DESC, id DESC
        """,
    ):
        key = (clean(row["client"]), clean(row["position"]))
        corrections[key] = {
            "promote": parse_json_list(row["promote_keywords_json"]),
            "suppress": parse_json_list(row["suppress_keywords_json"]),
            "target": parse_json_list(row["target_tags_json"]),
            "blocker": parse_json_list(row["blocker_tags_json"]),
            "evidence": parse_json_list(row["evidence_json"]),
        }
    return corrections


def load_position_profiles(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    profiles: dict[tuple[str, str], dict[str, Any]] = {}
    if not table_exists(conn, "position_profiles"):
        return profiles
    rows = safe_rows(
        conn,
        """
        SELECT client, position, hard_requirements_json, ability_keywords_json,
               target_companies_json, exclusion_tags_json, search_keywords_json,
               soft_preferences_json, pitch_points_json, risk_points_json,
               jd_analysis_summary
        FROM position_profiles
        ORDER BY datetime(updated_at) DESC, id DESC
        """,
    )
    for row in rows:
        key = (clean(row["client"]), clean(row["position"]))
        profiles[key] = {
            "hard_requirements": parse_json_list(row["hard_requirements_json"]),
            "ability_keywords": parse_json_list(row["ability_keywords_json"]),
            "target_companies": parse_json_list(row["target_companies_json"]),
            "exclusion_tags": parse_json_list(row["exclusion_tags_json"]),
            "search_keywords": parse_json_list(row["search_keywords_json"]),
            "soft_preferences": parse_json_list(row["soft_preferences_json"]),
            "pitch_points": parse_json_list(row["pitch_points_json"]),
            "risk_points": parse_json_list(row["risk_points_json"]),
            "summary": clean(row["jd_analysis_summary"]),
        }
    return profiles


def load_feedback_examples(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, list[str]]]:
    examples: dict[tuple[str, str], dict[str, list[str]]] = defaultdict(lambda: {"positive": [], "negative": []})
    if not table_exists(conn, "client_feedback_events"):
        return examples
    for row in safe_rows(
        conn,
        """
        SELECT client, position, candidate_name, candidate_company, feedback_type,
               feedback_detail, reason_tags_json
        FROM client_feedback_events
        ORDER BY datetime(COALESCE(feedback_time, created_at)) DESC, id DESC
        LIMIT 80
        """,
    ):
        key = (clean(row["client"]), clean(row["position"]))
        label = "positive" if clean(row["feedback_type"]) in POSITIVE_FEEDBACK else "negative"
        if clean(row["feedback_type"]) not in POSITIVE_FEEDBACK | NEGATIVE_FEEDBACK:
            continue
        detail = first_line(row["feedback_detail"] or row["reason_tags_json"], 40)
        name = clean(row["candidate_name"],) or "未识别人选"
        company = clean(row["candidate_company"])
        text = f"{name}{f'（{company}）' if company else ''}：{detail or clean(row['feedback_type'])}"
        examples[key][label].append(text)
    return examples


def load_recent_experiment_notes(conn: sqlite3.Connection) -> dict[tuple[str, str], list[str]]:
    notes: dict[tuple[str, str], list[str]] = defaultdict(list)
    if not table_exists(conn, "search_experiments"):
        return notes
    for row in safe_rows(
        conn,
        """
        SELECT client, position, query, result_count, viewed_count, extracted_count,
               recommended_count, reply_count, positive_reply_count, noise_notes
        FROM search_experiments
        ORDER BY datetime(COALESCE(updated_at, run_time, created_at)) DESC, id DESC
        LIMIT 80
        """,
    ):
        key = (clean(row["client"]), clean(row["position"]))
        viewed = int(row["viewed_count"] or 0)
        extracted = int(row["extracted_count"] or 0)
        recommended = int(row["recommended_count"] or 0)
        positive = int(row["positive_reply_count"] or 0)
        if positive:
            note = f"保留：{clean(row['query'])} 已产生正向回复"
        elif recommended:
            note = f"观察：{clean(row['query'])} 已有人选推荐，等回复后回填"
        elif viewed and not extracted:
            note = f"降噪：{clean(row['query'])} 查看 {viewed} 人但未入库"
        elif row["noise_notes"]:
            note = f"注意：{first_line(row['noise_notes'], 34)}"
        else:
            continue
        notes[key].append(note)
    return notes


def build_strategy_item(
    project: dict[str, Any],
    feedback_examples: dict[tuple[str, str], dict[str, list[str]]],
    experiment_notes: dict[tuple[str, str], list[str]],
    strategy_corrections: dict[tuple[str, str], dict[str, list[str]]],
    position_profiles: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    client, position = split_project(project)
    key = (client, position)
    feedback = feedback_examples.get(key, {"positive": [], "negative": []})
    correction = strategy_corrections.get(key, {})
    profile = position_profiles.get(key, {})
    score = strategy_score(project)
    if project.get("positive_feedback"):
        mode = "沿客户认可样本扩搜"
    elif project.get("negative_feedback"):
        mode = "先复盘负反馈再重搜"
    elif correction.get("promote") and int(project.get("outreach_events") or 0):
        mode = "沿已触达样本扩搜"
    elif int(project.get("active_candidates") or 0) < 3:
        mode = "补池优先"
    elif not project.get("search_experiments"):
        mode = "建立首轮搜索基线"
    else:
        mode = "优化关键词转化"
    return {
        "score": score,
        "project": display_project(client, position),
        "client": client,
        "position": position,
        "mode": mode,
        "reason": project_reason(project),
        "keywords": keyword_candidates(position, dedupe(correction.get("promote", []) + profile.get("search_keywords", []))),
        "filters": filter_suggestions(project),
        "hard_requirements": profile.get("hard_requirements", [])[:4],
        "soft_preferences": profile.get("soft_preferences", [])[:4],
        "target_companies": profile.get("target_companies", [])[:4],
        "pitch_points": profile.get("pitch_points", [])[:4],
        "profile_risks": profile.get("risk_points", [])[:4],
        "jd_summary": profile.get("summary", ""),
        "positive_examples": feedback.get("positive", [])[:3],
        "negative_examples": feedback.get("negative", [])[:3],
        "experiment_notes": dedupe(experiment_notes.get(key, []) + correction.get("evidence", []))[:4],
        "suppress_keywords": correction.get("suppress", [])[:4],
        "blockers": correction.get("blocker", [])[:4],
        "search_experiments": int(project.get("search_experiments") or 0),
        "outreach_events": int(project.get("outreach_events") or 0),
        "active_candidates": int(project.get("active_candidates") or 0),
        "advanced_candidates": int(project.get("advanced_candidates") or 0),
    }


def write_strategy_table(lines: list[str], items: list[dict[str, Any]], limit: int = 12) -> None:
    lines.extend(
        [
            "| 优先级 | 项目 | 搜索模式 | 为什么现在搜 | 建议关键词 |",
            "|---:|---|---|---|---|",
        ]
    )
    if not items:
        lines.append("| - | 暂无 | - | - | - |")
        return
    for item in items[:limit]:
        lines.append(
            f"| {item['score']} | {item['project'].replace('|', '｜')} | {item['mode']} | "
            f"{first_line(item['reason'], 52).replace('|', '｜')} | "
            f"{'、'.join(item['keywords'][:4]).replace('|', '｜')} |"
        )


def write_detail(lines: list[str], item: dict[str, Any]) -> None:
    lines.extend(
        [
            f"### {item['project']}",
            "",
            f"- 建议动作：{item['mode']}",
            f"- 推荐关键词：{'、'.join(item['keywords']) or '先用岗位名搜索'}",
            f"- 筛选建议：{'；'.join(item['filters'])}",
        ]
    )
    if item["hard_requirements"]:
        lines.append(f"- 硬性门槛：{'；'.join(item['hard_requirements'])}")
    if item["soft_preferences"]:
        lines.append(f"- 软性偏好：{'；'.join(item['soft_preferences'])}")
    if item["target_companies"]:
        lines.append(f"- 目标公司/背景：{'、'.join(item['target_companies'])}")
    if item["pitch_points"]:
        lines.append(f"- 沟通卖点：{'；'.join(item['pitch_points'])}")
    if item["profile_risks"]:
        lines.append(f"- 风险/待补：{'；'.join(item['profile_risks'])}")
    if item["positive_examples"]:
        lines.append(f"- 正样本参考：{'；'.join(item['positive_examples'])}")
    if item["negative_examples"]:
        lines.append(f"- 负反馈避坑：{'；'.join(item['negative_examples'])}")
    if item["experiment_notes"]:
        lines.append(f"- 历史搜索提醒：{'；'.join(item['experiment_notes'])}")
    if item["suppress_keywords"]:
        lines.append(f"- 降权关键词：{'、'.join(item['suppress_keywords'])}")
    if item["blockers"]:
        lines.append(f"- 阻力/风险：{'；'.join(item['blockers'])}")
    lines.append("")


def write_learning_watchlist(lines: list[str], items: list[dict[str, Any]], limit: int = 8) -> None:
    watch_items = [
        item for item in items
        if item["search_experiments"] or item["outreach_events"]
    ]
    watch_items = sorted(
        watch_items,
        key=lambda item: (item["outreach_events"], item["search_experiments"], item["advanced_candidates"]),
        reverse=True,
    )
    lines.extend(
        [
            "",
            "## 近期搜索学习/待观察",
            "",
            "| 项目 | 当前状态 | 下一步 | 可复用关键词 |",
            "|---|---|---|---|",
        ]
    )
    if not watch_items:
        lines.append("| 暂无 | - | - | - |")
        return
    for item in watch_items[:limit]:
        status = (
            f"{item['search_experiments']} 轮搜索，{item['outreach_events']} 条触达，"
            f"{item['advanced_candidates']} 个已推进人选"
        )
        if item["outreach_events"] and not item.get("positive_examples"):
            next_step = "先等候选人回复，补搜时只扩相近词"
        else:
            next_step = item["mode"]
        lines.append(
            f"| {item['project'].replace('|', '｜')} | {status} | "
            f"{first_line(next_step, 34).replace('|', '｜')} | "
            f"{'、'.join(item['keywords'][:4]).replace('|', '｜')} |"
        )


def write_report(projects: list[dict[str, Any]], conn: sqlite3.Connection, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘下一轮搜索策略_{stamp}.md"

    feedback_examples = load_feedback_examples(conn)
    experiment_notes = load_recent_experiment_notes(conn)
    strategy_corrections = load_strategy_corrections(conn)
    position_profiles = load_position_profiles(conn)
    candidates = [
        build_strategy_item(project, feedback_examples, experiment_notes, strategy_corrections, position_profiles)
        for project in projects
        if clean(project.get("client"))
        and clean(project.get("position"))
        and (
            int(project.get("open_position_rows") or 0)
            or int(project.get("search_experiments") or 0)
            or int(project.get("outreach_events") or 0)
        )
    ]
    candidates = sorted(candidates, key=lambda item: item["score"], reverse=True)
    top_items = [item for item in candidates if item["score"] > 0][:15]
    mode_counts = Counter(item["mode"] for item in top_items)

    lines = [
        "# 猎聘下一轮搜索策略",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 一句话结论",
        "",
    ]
    if top_items:
        lines.append(
            f"下一轮先做 {top_items[0]['project']}；原因是{top_items[0]['reason']}。"
        )
    else:
        lines.append("当前没有明显需要立刻补搜的在招岗位，先处理回复待办和客户反馈。")

    lines.extend(
        [
            "",
            "## 搜索优先级",
            "",
        ]
    )
    write_strategy_table(lines, top_items)
    write_learning_watchlist(lines, candidates)

    lines.extend(["", "## 搜索打法明细", ""])
    if top_items:
        for item in top_items[:8]:
            write_detail(lines, item)
    else:
        lines.append("- 暂无。")

    lines.extend(
        [
            "## 执行规则",
            "",
            "1. 每个岗位先跑一轮小样本：看 20-40 人，入库 5-8 个，再决定是否扩大。",
            "2. 搜完马上双击 `记录猎聘搜索实验.command`，先记录关键词、筛选、结果数、查看数和入库数。",
            "3. 有推荐和回复后再按搜索实验编号回填结果，下一轮策略会自动改口径。",
        ]
    )

    if mode_counts:
        lines.extend(
            [
                "",
                "## 本轮搜索类型分布",
                "",
                "- " + "、".join(f"{key} {value}" for key, value in mode_counts.items()),
            ]
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate next-round Liepin search strategy.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    conn = connect(db_path)
    try:
        projects, _recent_activity = collect_projects(conn)
        report = write_report(projects, conn, Path(args.output_dir).expanduser())
    finally:
        conn.close()

    print(
        json.dumps(
            {
                "ok": True,
                "projects": len(projects),
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
