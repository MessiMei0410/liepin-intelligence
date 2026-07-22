#!/usr/bin/env python3
"""Record or update one Liepin search round in search_experiments."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


SCHEMA = """
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
"""


EXTRA_COLUMNS = {
    "round_name": "TEXT",
    "viewed_count": "INTEGER",
    "status": "TEXT DEFAULT 'open'",
    "source_url": "TEXT",
    "updated_at": "TEXT",
}


RECORD_COLUMNS = [
    "client",
    "position",
    "channel",
    "round_name",
    "query",
    "filters_json",
    "result_count",
    "viewed_count",
    "extracted_count",
    "recommended_count",
    "reply_count",
    "positive_reply_count",
    "status",
    "source_url",
    "noise_notes",
    "run_time",
]


FILTER_ARGS = {
    "city": "city",
    "expected_city": "expected_city",
    "education": "education",
    "experience": "experience",
    "company": "company",
    "page_scope": "page_scope",
}


STATUS_RANK = {
    "open": 0,
    "tracking": 1,
    "replied": 2,
    "learned": 3,
}


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return set()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)
    existing = column_names(conn, "search_experiments")
    for name, definition in EXTRA_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE search_experiments ADD COLUMN {name} {definition}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_search_experiments_client_position ON search_experiments(client, position)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_search_experiments_status_time ON search_experiments(status, run_time)"
    )
    conn.execute(
        """
        UPDATE search_experiments
        SET status = 'open'
        WHERE status IS NULL OR status = ''
        """
    )
    conn.execute(
        """
        UPDATE search_experiments
        SET updated_at = COALESCE(NULLIF(updated_at, ''), created_at, run_time, datetime('now','localtime'))
        WHERE updated_at IS NULL OR updated_at = ''
        """
    )
    conn.commit()


def parse_json_object(text: str | None, label: str = "JSON") -> dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{label} 不是有效 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{label} 必须是对象，例如：{{\"city\":\"苏州\"}}")
    return value


def parse_filters(args: argparse.Namespace, existing: sqlite3.Row | None = None) -> dict[str, Any]:
    filters = parse_json_object(existing["filters_json"], "历史筛选") if existing else {}
    if args.filters_json:
        filters.update(parse_json_object(args.filters_json, "--filters-json"))
    for arg_name, filter_key in FILTER_ARGS.items():
        value = getattr(args, arg_name, None)
        if value not in (None, ""):
            filters[filter_key] = value
    return filters


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(value.split())


def choose(args: argparse.Namespace, name: str, existing: sqlite3.Row | None, default: Any = None) -> Any:
    value = getattr(args, name, None)
    if value is not None:
        return value
    if existing is not None and name in existing.keys():
        return existing[name]
    return default


def infer_status(record: dict[str, Any]) -> str:
    if record.get("positive_reply_count"):
        return "learned"
    if record.get("reply_count"):
        return "replied"
    if record.get("recommended_count"):
        return "tracking"
    return "open"


def normalize_record(args: argparse.Namespace, existing: sqlite3.Row | None = None) -> dict[str, Any]:
    query = clean_text(choose(args, "query", existing, "")) or ""
    if not query:
        raise SystemExit("需要提供关键词。可用 --query，或双击记录入口后按提示填写。")

    filters = parse_filters(args, existing)
    record = {
        "client": clean_text(choose(args, "client", existing, "")) or "",
        "position": clean_text(choose(args, "position", existing, "")) or "",
        "channel": choose(args, "channel", existing, "liepin") or "liepin",
        "round_name": clean_text(choose(args, "round_name", existing, "")) or "",
        "query": query,
        "filters_json": json.dumps(filters, ensure_ascii=False, sort_keys=True),
        "result_count": choose(args, "result_count", existing),
        "viewed_count": choose(args, "viewed_count", existing),
        "extracted_count": choose(args, "extracted_count", existing),
        "recommended_count": choose(args, "recommended_count", existing, 0) or 0,
        "reply_count": choose(args, "reply_count", existing, 0) or 0,
        "positive_reply_count": choose(args, "positive_reply_count", existing, 0) or 0,
        "status": "",
        "source_url": clean_text(choose(args, "source_url", existing, "")) or "",
        "noise_notes": clean_text(choose(args, "noise_notes", existing, "")) or "",
        "run_time": choose(args, "run_time", existing, datetime.now().isoformat(timespec="seconds")),
    }
    inferred_status = infer_status(record)
    requested_status = clean_text(getattr(args, "status", None))
    existing_status = clean_text(existing["status"]) if existing is not None and "status" in existing.keys() else ""
    if requested_status:
        record["status"] = requested_status
    elif existing_status and STATUS_RANK.get(inferred_status, 0) <= STATUS_RANK.get(existing_status, 0):
        record["status"] = existing_status
    else:
        record["status"] = inferred_status
    return record


def insert_record(conn: sqlite3.Connection, record: dict[str, Any]) -> int:
    columns = ", ".join(RECORD_COLUMNS)
    placeholders = ", ".join(f":{column}" for column in RECORD_COLUMNS)
    cursor = conn.execute(
        f"""
        INSERT INTO search_experiments ({columns}, updated_at)
        VALUES ({placeholders}, datetime('now','localtime'))
        """,
        record,
    )
    conn.commit()
    return int(cursor.lastrowid)


def update_record(conn: sqlite3.Connection, experiment_id: int, record: dict[str, Any]) -> None:
    assignments = ", ".join(f"{column}=:{column}" for column in RECORD_COLUMNS)
    conn.execute(
        f"""
        UPDATE search_experiments
        SET {assignments}, updated_at=datetime('now','localtime')
        WHERE id=:id
        """,
        {**record, "id": experiment_id},
    )
    conn.commit()


def load_record(conn: sqlite3.Connection, experiment_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM search_experiments WHERE id = ?",
        (experiment_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"没有找到搜索实验 #{experiment_id}")
    return row


def load_position(conn: sqlite3.Connection, position_id: int) -> sqlite3.Row | None:
    try:
        return conn.execute(
            """
            SELECT id, client, title, status, gap
            FROM positions
            WHERE id = ?
            """,
            (position_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def recent_positions(conn: sqlite3.Connection, limit: int = 18) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            """
            SELECT id, client, title, status, gap
            FROM positions
            WHERE COALESCE(status, 'open') = 'open'
            ORDER BY datetime(updated_at) DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def apply_position(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    if not args.position_id:
        return
    row = load_position(conn, args.position_id)
    if row is None:
        raise SystemExit(f"没有找到岗位 #{args.position_id}")
    args.client = args.client or row["client"]
    args.position = args.position or row["title"]


def prompt_text(label: str, default: str | None = None, required: bool = False) -> str:
    default_text = "" if default is None else str(default)
    suffix = f" [{default_text}]" if default_text else ""
    while True:
        value = input(f"{label}{suffix}：").strip()
        if value:
            return value
        if default_text:
            return default_text
        if not required:
            return ""
        print("这个必填，填一下就行。")


def prompt_int(label: str, default: int | None = None) -> int | None:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        value = input(f"{label}{suffix}：").strip()
        if not value:
            return default
        try:
            parsed = int(value)
        except ValueError:
            print("这里填数字，留空也可以。")
            continue
        if parsed < 0:
            print("数量不能小于 0。")
            continue
        return parsed


def choose_position_interactively(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    if args.client or args.position or args.position_id:
        return
    positions = recent_positions(conn)
    if not positions:
        return
    print("\n可选岗位（留空可手填）：")
    for row in positions:
        gap = f"，缺口 {row['gap']}" if row["gap"] is not None else ""
        print(f"  {row['id']}. {row['client']} / {row['title']}（{row['status'] or 'open'}{gap}）")
    selected = prompt_text("选择岗位编号", "")
    if not selected:
        return
    try:
        position_id = int(selected)
    except ValueError:
        print("没识别成编号，改为手填客户和岗位。")
        return
    args.position_id = position_id
    apply_position(args, conn)


def apply_interactive(args: argparse.Namespace, conn: sqlite3.Connection, existing: sqlite3.Row | None) -> None:
    choose_position_interactively(args, conn)
    args.client = prompt_text("客户", choose(args, "client", existing, "") or "")
    args.position = prompt_text("岗位", choose(args, "position", existing, "") or "")
    args.round_name = prompt_text("这一轮怎么命名", choose(args, "round_name", existing, "") or "")
    args.query = prompt_text("猎聘搜索关键词", choose(args, "query", existing, "") or "", required=True)
    args.city = prompt_text("筛选城市", getattr(args, "city", None) or parse_json_object(existing["filters_json"], "历史筛选").get("city", "") if existing else getattr(args, "city", None) or "")
    args.expected_city = prompt_text("期望城市", getattr(args, "expected_city", None) or parse_json_object(existing["filters_json"], "历史筛选").get("expected_city", "") if existing else getattr(args, "expected_city", None) or "")
    args.education = prompt_text("学历要求", getattr(args, "education", None) or parse_json_object(existing["filters_json"], "历史筛选").get("education", "") if existing else getattr(args, "education", None) or "")
    args.experience = prompt_text("年限要求", getattr(args, "experience", None) or parse_json_object(existing["filters_json"], "历史筛选").get("experience", "") if existing else getattr(args, "experience", None) or "")
    args.company = prompt_text("目标公司/排除公司备注", getattr(args, "company", None) or parse_json_object(existing["filters_json"], "历史筛选").get("company", "") if existing else getattr(args, "company", None) or "")
    args.page_scope = prompt_text("这轮看了哪些范围", getattr(args, "page_scope", None) or parse_json_object(existing["filters_json"], "历史筛选").get("page_scope", "") if existing else getattr(args, "page_scope", None) or "")
    args.result_count = prompt_int("页面结果数", choose(args, "result_count", existing))
    args.viewed_count = prompt_int("实际查看人数", choose(args, "viewed_count", existing))
    args.extracted_count = prompt_int("入库/入围人数", choose(args, "extracted_count", existing))
    args.recommended_count = prompt_int("已推荐人数", choose(args, "recommended_count", existing, 0))
    args.reply_count = prompt_int("候选人回复人数", choose(args, "reply_count", existing, 0))
    args.positive_reply_count = prompt_int("正向/可继续回复人数", choose(args, "positive_reply_count", existing, 0))
    args.noise_notes = prompt_text("噪音或复盘备注", choose(args, "noise_notes", existing, "") or "")
    args.source_url = prompt_text("猎聘页面链接", choose(args, "source_url", existing, "") or "")
    args.status = prompt_text("状态（open/tracking/replied/learned）", choose(args, "status", existing, "") or "")


def pct(numerator: int | None, denominator: int | None) -> str:
    if numerator is None or denominator is None or denominator == 0:
        return "暂无"
    return f"{numerator / denominator:.1%}"


def advice(record: dict[str, Any]) -> list[str]:
    result_count = record.get("result_count")
    viewed_count = record.get("viewed_count")
    extracted_count = record.get("extracted_count")
    recommended_count = record.get("recommended_count")
    reply_count = record.get("reply_count")
    positive_reply_count = record.get("positive_reply_count")

    notes: list[str] = []
    if isinstance(result_count, int):
        if result_count >= 200:
            notes.append("结果量偏大，下轮建议加城市、学历、目标公司或更具体技术词。")
        elif result_count < 10:
            notes.append("结果量偏小，下轮建议去掉限制词，先保留核心岗位词扩池。")
        else:
            notes.append("结果量适中，可以优先看前两页活跃候选人。")
    if viewed_count and result_count and viewed_count / max(result_count, 1) < 0.08:
        notes.append("页面结果很多但查看比例偏低，建议先用更强筛选缩小人群。")
    if viewed_count and extracted_count is not None:
        if extracted_count == 0:
            notes.append("看过但没有入库，关键词可能噪音较高，建议记录排除原因。")
        elif extracted_count / max(viewed_count, 1) >= 0.3:
            notes.append("查看到入库转化不错，这组关键词值得继续扩展相近词。")
    if extracted_count and recommended_count is not None:
        if recommended_count == 0:
            notes.append("已有入库但无推荐，需复盘硬门槛或补关键验证问题。")
        elif recommended_count / max(extracted_count, 1) < 0.2:
            notes.append("入库到推荐转化偏低，下轮关键词可能覆盖了太多边缘人群。")
    if reply_count is not None and recommended_count:
        if reply_count == 0:
            notes.append("已推荐但暂无回复，后续先观察话术和岗位卖点是否需要调整。")
        elif reply_count / max(recommended_count, 1) >= 0.4:
            notes.append("推荐到回复转化不错，可把这轮筛选条件作为该岗位优先打法。")
    if reply_count and positive_reply_count is not None:
        if positive_reply_count == 0:
            notes.append("已有回复但无正向回复，后续要调整开场话术或岗位卖点。")
        elif positive_reply_count / max(reply_count, 1) >= 0.5:
            notes.append("正向回复占比较高，这组关键词/人群值得保留。")
    return notes or ["等有推荐和回复后，再回填这轮搜索的转化率。"]


def format_count(value: Any) -> str:
    return str(value) if value is not None else "未填"


def write_receipt(
    output_dir: Path,
    record_id: int | None,
    record: dict[str, Any],
    dry_run: bool,
    updated: bool,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "dryrun" if dry_run else f"id{record_id}"
    path = output_dir / f"猎聘搜索实验记录_{suffix}_{stamp}.md"
    filters = parse_json_object(record["filters_json"], "筛选")
    storage_status = "未入库（干跑验证）" if dry_run else f"已{'更新' if updated else '新增'} #{record_id}"

    lines = [
        "# 猎聘搜索实验记录",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"入库状态：{storage_status}",
        "",
        "## 搜索轮次",
        "",
        f"- 客户：{record['client'] or '未填'}",
        f"- 岗位：{record['position'] or '未填'}",
        f"- 轮次：{record['round_name'] or '未命名'}",
        f"- 状态：{record['status'] or 'open'}",
        f"- 渠道：{record['channel']}",
        f"- 关键词：{record['query']}",
        f"- 筛选：{json.dumps(filters, ensure_ascii=False) if filters else '无'}",
        f"- 页面结果数：{format_count(record['result_count'])}",
        f"- 实际查看人数：{format_count(record['viewed_count'])}",
        f"- 入库/入围人数：{format_count(record['extracted_count'])}",
        f"- 推荐数：{format_count(record['recommended_count'])}",
        f"- 回复数：{format_count(record['reply_count'])}",
        f"- 正向回复数：{format_count(record['positive_reply_count'])}",
        f"- 搜索时间：{record['run_time']}",
        f"- 页面链接：{record['source_url'] or '无'}",
        f"- 噪音备注：{record['noise_notes'] or '无'}",
        "",
        "## 转化速览",
        "",
        f"- 查看 / 结果：{pct(record['viewed_count'], record['result_count'])}",
        f"- 入库 / 查看：{pct(record['extracted_count'], record['viewed_count'])}",
        f"- 推荐 / 入库：{pct(record['recommended_count'], record['extracted_count'])}",
        f"- 回复 / 推荐：{pct(record['reply_count'], record['recommended_count'])}",
        f"- 正向 / 回复：{pct(record['positive_reply_count'], record['reply_count'])}",
        "",
        "## 下轮建议",
        "",
    ]
    lines.extend(f"- {item}" for item in advice(record))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def list_recent(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            id, client, position, round_name, query, result_count, viewed_count,
            extracted_count, recommended_count, reply_count, positive_reply_count,
            status, run_time, updated_at
        FROM search_experiments
        ORDER BY datetime(run_time) DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record or update a Liepin search experiment.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--interactive", "-i", action="store_true", help="按提示填写")
    parser.add_argument("--list", action="store_true", help="查看最近搜索实验")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--experiment-id", "--id", dest="experiment_id", type=int, help="回填/更新某一轮搜索")
    parser.add_argument("--position-id", type=int, help="从本地岗位池选择岗位")
    parser.add_argument("--client")
    parser.add_argument("--position")
    parser.add_argument("--channel")
    parser.add_argument("--round-name")
    parser.add_argument("--query")
    parser.add_argument("--filters-json")
    parser.add_argument("--city")
    parser.add_argument("--expected-city")
    parser.add_argument("--education")
    parser.add_argument("--experience")
    parser.add_argument("--company")
    parser.add_argument("--page-scope", help="例如：前2页、前40人、全量")
    parser.add_argument("--result-count", type=int)
    parser.add_argument("--viewed-count", type=int)
    parser.add_argument("--extracted-count", "--saved-count", dest="extracted_count", type=int)
    parser.add_argument("--recommended-count", type=int)
    parser.add_argument("--reply-count", type=int)
    parser.add_argument("--positive-reply-count", type=int)
    parser.add_argument("--status", help="open/tracking/replied/learned")
    parser.add_argument("--source-url")
    parser.add_argument("--noise-notes")
    parser.add_argument("--run-time")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    conn = connect(Path(args.db).expanduser())
    try:
        ensure_schema(conn)
        if args.list:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "records": list_recent(conn, args.limit),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        existing = load_record(conn, args.experiment_id) if args.experiment_id else None
        apply_position(args, conn)
        if args.interactive:
            apply_interactive(args, conn, existing)

        record = normalize_record(args, existing)
        record_id = args.experiment_id
        updated = bool(args.experiment_id)
        if not args.dry_run:
            if args.experiment_id:
                update_record(conn, args.experiment_id, record)
            else:
                record_id = insert_record(conn, record)
    finally:
        conn.close()

    receipt = write_receipt(Path(args.output_dir).expanduser(), record_id, record, args.dry_run, updated)
    print(
        json.dumps(
            {
                "ok": True,
                "dry_run": args.dry_run,
                "updated": updated,
                "record_id": record_id,
                "record": record,
                "receipt": str(receipt),
                "advice": advice(record),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
