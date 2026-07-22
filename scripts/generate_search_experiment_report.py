#!/usr/bin/env python3
"""Generate a strategy report from Liepin search experiments."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from record_search_experiment import DEFAULT_DB, DEFAULT_OUTPUT_DIR, connect, ensure_schema, pct


from typing import Union

ExperimentRow = Union[sqlite3.Row, dict[str, Any]]

STATUS_RANK = {
    "open": 0,
    "tracking": 1,
    "replied": 2,
    "learned": 3,
}


def parse_json_object(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def load_experiments(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            id, client, position, channel, round_name, query, filters_json,
            result_count, viewed_count, extracted_count, recommended_count,
            reply_count, positive_reply_count, status, source_url, noise_notes,
            run_time, created_at, updated_at
        FROM search_experiments
        ORDER BY datetime(run_time) DESC, id DESC
        """
    ).fetchall()


def clean(text: Any, fallback: str = "未填") -> str:
    value = " ".join((text or "").split())
    return value or fallback


def first_line(text: Any, limit: int = 60) -> str:
    value = clean(text, "")
    return value[:limit] + ("..." if len(value) > limit else "")


def value(row: ExperimentRow, key: str, default: Any = None) -> Any:
    if isinstance(row, sqlite3.Row):
        return row[key] if key in row.keys() else default
    return row.get(key, default)


def num(row: ExperimentRow, key: str) -> int:
    return int((row[key] if isinstance(row, sqlite3.Row) else row.get(key)) or 0)


def sum_field(rows: list[ExperimentRow], key: str) -> int:
    return sum(num(row, key) for row in rows)


def project_key(row: ExperimentRow) -> str:
    client = clean(value(row, "client"), "")
    position = clean(value(row, "position"), "")
    if client and position:
        return f"{client}/{position}"
    return client or position or "未标项目"


def score_experiment(row: ExperimentRow) -> float:
    extracted = num(row, "extracted_count")
    viewed = num(row, "viewed_count")
    recommended = num(row, "recommended_count")
    replies = num(row, "reply_count")
    positive = num(row, "positive_reply_count")
    score = 0.0
    if viewed:
        score += min(extracted / viewed, 1.0) * 35
    if extracted:
        score += min(recommended / extracted, 1.0) * 30
    if recommended:
        score += min(replies / recommended, 1.0) * 20
    if replies:
        score += min(positive / replies, 1.0) * 15
    return round(score, 1)


def normalized_filters(row: ExperimentRow) -> str:
    filters = parse_json_object(value(row, "filters_json"))
    return json.dumps(filters, ensure_ascii=False, sort_keys=True)


def experiment_identity(row: ExperimentRow) -> tuple[str, str, str, str, str]:
    return (
        clean(value(row, "client"), ""),
        clean(value(row, "position"), ""),
        clean(value(row, "channel"), "liepin"),
        clean(value(row, "query"), ""),
        normalized_filters(row),
    )


def row_time(row: ExperimentRow) -> str:
    return clean(value(row, "updated_at") or value(row, "run_time") or value(row, "created_at"), "")


def is_newer(candidate: ExperimentRow, current: ExperimentRow) -> bool:
    return (row_time(candidate), num(candidate, "id")) > (row_time(current), num(current, "id"))


def best_status(a: str, b: str) -> str:
    return a if STATUS_RANK.get(a, 0) >= STATUS_RANK.get(b, 0) else b


def consolidate_experiments(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Merge accidental duplicate records for the same search condition.

    A search can be recorded twice while counts are being backfilled. For strategy
    learning, that should behave like one experiment with the strongest observed
    counters, not two separate searches.
    """

    grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    numeric_fields = [
        "result_count",
        "viewed_count",
        "extracted_count",
        "recommended_count",
        "reply_count",
        "positive_reply_count",
    ]
    for row in rows:
        key = experiment_identity(row)
        row_dict = dict(row)
        row_dict["source_ids"] = [row["id"]]
        row_dict["duplicate_count"] = 1
        if key not in grouped:
            grouped[key] = row_dict
            continue

        existing = grouped[key]
        existing["source_ids"].append(row["id"])
        existing["duplicate_count"] += 1
        for field in numeric_fields:
            if value(row, field) is not None:
                existing[field] = max(num(existing, field), num(row, field))
        existing["status"] = best_status(clean(existing.get("status"), "open"), clean(row["status"], "open"))
        if row["noise_notes"] and row["noise_notes"] not in clean(existing.get("noise_notes"), ""):
            existing["noise_notes"] = "；".join(
                item for item in [clean(existing.get("noise_notes"), ""), clean(row["noise_notes"], "")] if item
            )
        if is_newer(row, existing):
            for field in ["id", "round_name", "source_url", "run_time", "created_at", "updated_at", "filters_json"]:
                existing[field] = row[field]

    return sorted(grouped.values(), key=lambda item: (row_time(item), num(item, "id")), reverse=True)


def duplicate_summaries(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        groups[experiment_identity(row)].append(row)
    summaries: list[dict[str, Any]] = []
    for grouped_rows in groups.values():
        if len(grouped_rows) < 2:
            continue
        latest = max(grouped_rows, key=lambda row: (row_time(row), num(row, "id")))
        summaries.append(
            {
                "project": project_key(latest),
                "query": clean(latest["query"]),
                "ids": [row["id"] for row in sorted(grouped_rows, key=lambda item: item["id"])],
                "records": len(grouped_rows),
                "recommended": max(num(row, "recommended_count") for row in grouped_rows),
                "replies": max(num(row, "reply_count") for row in grouped_rows),
            }
        )
    return sorted(summaries, key=lambda item: item["records"], reverse=True)


def aggregate_by_project(rows: list[ExperimentRow]) -> list[dict[str, Any]]:
    groups: dict[str, list[ExperimentRow]] = defaultdict(list)
    for row in rows:
        groups[project_key(row)].append(row)

    summaries = []
    for project, project_rows in groups.items():
        summaries.append(
            {
                "project": project,
                "experiments": len(project_rows),
                "viewed": sum_field(project_rows, "viewed_count"),
                "extracted": sum_field(project_rows, "extracted_count"),
                "recommended": sum_field(project_rows, "recommended_count"),
                "replies": sum_field(project_rows, "reply_count"),
                "positive": sum_field(project_rows, "positive_reply_count"),
            }
        )
    return sorted(
        summaries,
        key=lambda item: (item["positive"], item["replies"], item["recommended"], item["experiments"]),
        reverse=True,
    )


def classify_learning(rows: list[ExperimentRow]) -> tuple[list[ExperimentRow], list[ExperimentRow]]:
    ranked = sorted(rows, key=score_experiment, reverse=True)
    good = [
        row for row in ranked
        if num(row, "positive_reply_count") > 0
        or (num(row, "recommended_count") > 0 and score_experiment(row) >= 35)
        or (num(row, "viewed_count") > 0 and num(row, "extracted_count") / max(num(row, "viewed_count"), 1) >= 0.3)
    ]
    noisy = [
        row for row in sorted(rows, key=lambda item: (num(item, "result_count"), -score_experiment(item)), reverse=True)
        if (
            num(row, "result_count") >= 120
            and num(row, "viewed_count") > 0
            and num(row, "extracted_count") / max(num(row, "viewed_count"), 1) < 0.12
        )
        or (
            num(row, "extracted_count") >= 5
            and num(row, "recommended_count") == 0
        )
    ]
    return good[:8], noisy[:8]


def filters_text(row: ExperimentRow) -> str:
    filters = parse_json_object(value(row, "filters_json"))
    if not filters:
        return "无"
    preferred = ["city", "expected_city", "education", "experience", "company", "page_scope"]
    parts = [f"{key}={filters[key]}" for key in preferred if filters.get(key)]
    parts.extend(f"{key}={value}" for key, value in filters.items() if key not in preferred and value)
    return "；".join(parts) or "无"


def record_label(row: ExperimentRow) -> str:
    ids = value(row, "source_ids", [])
    if ids:
        ids_text = ",".join(f"#{item}" for item in ids)
        if num(row, "duplicate_count") > 1:
            return f"{ids_text}（合并）"
        return ids_text
    return f"#{num(row, 'id')}"


def write_latest_table(lines: list[str], rows: list[ExperimentRow]) -> None:
    lines.extend(
        [
            "| 记录 | 项目 | 轮次 | 关键词 | 筛选 | 查看/结果 | 入库/查看 | 推荐 | 回复 | 状态 |",
            "|---:|---|---|---|---|---:|---:|---:|---:|---|",
        ]
    )
    for row in rows[:12]:
        lines.append(
            "| {record} | {project} | {round_name} | {query} | {filters} | {view_rate} | {save_rate} | {recommended} | {replies} | {status} |".format(
                record=record_label(row),
                project=project_key(row).replace("|", "｜"),
                round_name=clean(value(row, "round_name"), "未命名").replace("|", "｜"),
                query=first_line(value(row, "query"), 34).replace("|", "｜"),
                filters=first_line(filters_text(row), 42).replace("|", "｜"),
                view_rate=pct(value(row, "viewed_count"), value(row, "result_count")),
                save_rate=pct(value(row, "extracted_count"), value(row, "viewed_count")),
                recommended=num(row, "recommended_count"),
                replies=num(row, "reply_count"),
                status=clean(value(row, "status"), "open"),
            )
        )


def write_report(rows: list[sqlite3.Row], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘搜索实验复盘_{stamp}.md"

    active_rows = consolidate_experiments(rows)
    duplicates = duplicate_summaries(rows)
    status_counts = Counter(clean(value(row, "status"), "open") for row in active_rows)
    viewed = sum_field(active_rows, "viewed_count")
    result_count = sum_field(active_rows, "result_count")
    extracted = sum_field(active_rows, "extracted_count")
    recommended = sum_field(active_rows, "recommended_count")
    replies = sum_field(active_rows, "reply_count")
    positive = sum_field(active_rows, "positive_reply_count")
    projects = aggregate_by_project(active_rows)
    good, noisy = classify_learning(active_rows)

    lines = [
        "# 猎聘搜索实验复盘",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 当前状态",
        "",
    ]

    if not active_rows:
        lines.extend(
            [
                "- 暂无真实搜索实验记录。",
                "- 下次在猎聘完成一轮搜索后，双击 `记录猎聘搜索实验.command`，把关键词、筛选、结果数、查看人数、入库人数记下来。",
                "- 后续有推荐和回复后，再用同一个编号回填结果，系统就能判断哪组关键词有效。",
            ]
        )
    else:
        lines.extend(
            [
                f"- 已记录搜索实验：{len(rows)} 条原始记录，合并后 {len(active_rows)} 个有效轮次",
                f"- 涉及项目：{len(projects)} 个",
                f"- 同关键词重复记录：{len(rows) - len(active_rows)} 条",
                f"- 状态分布：{'、'.join(f'{key} {value}' for key, value in status_counts.items())}",
                f"- 查看 / 结果：{pct(viewed, result_count)}",
                f"- 入库 / 查看：{pct(extracted, viewed)}",
                f"- 推荐 / 入库：{pct(recommended, extracted)}",
                f"- 回复 / 推荐：{pct(replies, recommended)}",
                f"- 正向 / 回复：{pct(positive, replies)}",
            ]
        )

    lines.extend(["", "## 最近搜索轮次", ""])
    if active_rows:
        write_latest_table(lines, active_rows)
    else:
        lines.append("- 暂无。")

    if duplicates:
        lines.extend(["", "## 已自动合并的重复记录", ""])
        lines.extend(
            [
                "| 项目 | 关键词 | 原记录 | 推荐 | 回复 |",
                "|---|---|---|---:|---:|",
            ]
        )
        for item in duplicates[:10]:
            ids = "、".join(f"#{record_id}" for record_id in item["ids"])
            lines.append(
                f"| {item['project'].replace('|', '｜')} | {item['query'].replace('|', '｜')} | "
                f"{ids} | {item['recommended']} | {item['replies']} |"
            )

    lines.extend(["", "## 项目汇总", ""])
    if projects:
        lines.extend(
            [
                "| 项目 | 搜索轮次 | 查看 | 入库 | 推荐 | 回复 | 正向 |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for item in projects[:12]:
            lines.append(
                f"| {item['project'].replace('|', '｜')} | {item['experiments']} | {item['viewed']} | "
                f"{item['extracted']} | {item['recommended']} | {item['replies']} | {item['positive']} |"
            )
    else:
        lines.append("- 暂无。")

    lines.extend(["", "## 有效打法", ""])
    if good:
        for row in good:
            lines.append(
                f"- {record_label(row)} {project_key(row)}｜{clean(value(row, 'query'))}｜得分 {score_experiment(row)}｜"
                f"入库/查看 {pct(value(row, 'extracted_count'), value(row, 'viewed_count'))}，"
                f"推荐/入库 {pct(value(row, 'recommended_count'), value(row, 'extracted_count'))}。"
            )
    else:
        lines.append("- 还没有足够数据判断有效打法。")

    lines.extend(["", "## 需要降噪", ""])
    if noisy:
        for row in noisy:
            lines.append(
                f"- {record_label(row)} {project_key(row)}｜{clean(value(row, 'query'))}｜{filters_text(row)}｜"
                f"结果 {num(row, 'result_count')}，查看 {num(row, 'viewed_count')}，入库 {num(row, 'extracted_count')}。"
            )
    else:
        lines.append("- 暂无明显噪音轮次。")

    lines.extend(
        [
            "",
            "## 下一步建议",
            "",
        ]
    )
    if not active_rows:
        lines.append("1. 先记录下一轮真实猎聘搜索，哪怕只填关键词、筛选、查看人数和入库人数也可以。")
    else:
        lines.append("1. 搜索刚结束先记录“结果数/查看人数/入库人数”，后续同关键词回填会自动合并进有效轮次。")
        lines.append("2. 有推荐或回复后，用原记录编号回填推荐数、回复数、正向回复数。")
        lines.append("3. 下轮搜索优先复用“有效打法”，对“需要降噪”的关键词增加城市、公司或技术限制。")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a Liepin search experiment report.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    conn = connect(Path(args.db).expanduser())
    try:
        ensure_schema(conn)
        experiments = load_experiments(conn)
        effective_experiments = len(consolidate_experiments(experiments))
    finally:
        conn.close()

    report = write_report(experiments, Path(args.output_dir).expanduser())
    print(
        json.dumps(
            {
                "ok": True,
                "experiments": len(experiments),
                "effective_experiments": effective_experiments,
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
