#!/usr/bin/env python3
"""Sync accepted Liepin reply assistant samples into the talent pool DB."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from export_reply_assistant_samples import (
    DEFAULT_EXTENSION_ID,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PROFILE_DIR,
    extension_storage_dir,
    read_samples_from_leveldb,
)


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
ACCEPT_EVENT_KEY = b"liepinReplyAssistantOutreachEvents"


SCHEMA = """
CREATE TABLE IF NOT EXISTS reply_assistant_samples (
    sample_id TEXT PRIMARY KEY,
    accepted_at TEXT,
    candidate_name TEXT,
    candidate_title TEXT,
    latest_message TEXT,
    strategy_key TEXT,
    strategy_label TEXT,
    score INTEGER DEFAULT 0,
    grade TEXT,
    project_client TEXT,
    project_position TEXT,
    project_confidence TEXT,
    project_rule TEXT,
    original_draft TEXT,
    edited_draft TEXT,
    changed INTEGER DEFAULT 0,
    length_delta INTEGER DEFAULT 0,
    reasons_json TEXT DEFAULT '[]',
    missing_json TEXT DEFAULT '[]',
    risk_json TEXT DEFAULT '[]',
    url TEXT,
    raw_json TEXT NOT NULL,
    source TEXT,
    synced_at TEXT DEFAULT (datetime('now','localtime'))
)
"""


INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_reply_assistant_samples_strategy ON reply_assistant_samples(strategy_key)",
    "CREATE INDEX IF NOT EXISTS idx_reply_assistant_samples_project ON reply_assistant_samples(project_client, project_position)",
    "CREATE INDEX IF NOT EXISTS idx_reply_assistant_samples_accepted_at ON reply_assistant_samples(accepted_at)",
]


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)
    for stmt in INDEXES:
        conn.execute(stmt)
    conn.commit()


def newest_export(output_dir: Path) -> Path | None:
    files = [
        path
        for path in output_dir.glob("猎聘话术采纳样本*.json")
        if path.is_file()
    ]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def load_samples(args: argparse.Namespace) -> tuple[list[dict[str, Any]], str]:
    if args.json_file:
        path = Path(args.json_file).expanduser()
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data.get("samples") or []), str(path)

    storage_dir = extension_storage_dir(Path(args.profile_dir).expanduser(), args.extension_id)
    try:
        samples = read_samples_from_leveldb(storage_dir)
        accept_events = read_accept_events_from_leveldb(storage_dir)
        return merge_accept_event_samples(list(samples), accept_events), str(storage_dir)
    except SystemExit:
        latest = newest_export(Path(args.output_dir).expanduser())
        if latest is None:
            return [], str(storage_dir)
        data = json.loads(latest.read_text(encoding="utf-8"))
        return list(data.get("samples") or []), str(latest)


def compact(text: Any) -> str:
    return " ".join(str(text or "").split())


def decode_accept_event_arrays_from_bytes(data: bytes) -> list[list[dict[str, Any]]]:
    decoder = json.JSONDecoder()
    arrays: list[list[dict[str, Any]]] = []
    offset = 0
    while True:
        key_pos = data.find(ACCEPT_EVENT_KEY, offset)
        if key_pos < 0:
            break
        json_start = data.find(b"[", key_pos + len(ACCEPT_EVENT_KEY), key_pos + len(ACCEPT_EVENT_KEY) + 200)
        offset = key_pos + len(ACCEPT_EVENT_KEY)
        if json_start < 0:
            continue
        text = data[json_start:].decode("utf-8", errors="ignore")
        try:
            value, _ = decoder.raw_decode(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            arrays.append(value)
    return arrays


def newest_event_time(events: list[dict[str, Any]]) -> str:
    times = [str(item.get("eventAt", "")) for item in events if item.get("eventAt")]
    return max(times) if times else ""


def read_accept_events_from_leveldb(storage_dir: Path) -> list[dict[str, Any]]:
    if not storage_dir.exists():
        return []

    candidates: list[list[dict[str, Any]]] = []
    for path in storage_dir.iterdir():
        if path.suffix not in {".log", ".ldb"}:
            continue
        try:
            candidates.extend(decode_accept_event_arrays_from_bytes(path.read_bytes()))
        except OSError:
            continue
    if not candidates:
        return []
    events = max(candidates, key=lambda arr: (len(arr), newest_event_time(arr)))
    return [
        event
        for event in events
        if event.get("eventType") == "reply_assistant_accept"
        and (event.get("draft") or event.get("messageSummary"))
    ]


def sample_dedupe_key(sample: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(sample.get("acceptedAt") or ""),
        str(sample.get("candidateName") or ""),
        compact(sample.get("editedDraft") or sample.get("draft") or sample.get("messageSummary")),
        str(sample.get("strategyKey") or ""),
    )


def sample_near_dedupe_key(sample: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(sample.get("candidateName") or ""),
        compact(sample.get("editedDraft") or sample.get("draft") or sample.get("messageSummary")),
        str(sample.get("strategyKey") or ""),
        str((sample.get("project") if isinstance(sample.get("project"), dict) else {}).get("position") or ""),
    )


def accepted_epoch_ms(sample: dict[str, Any]) -> int | None:
    raw = str(sample.get("acceptedAt") or "")
    if not raw:
        return None
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def sample_from_accept_event(event: dict[str, Any]) -> dict[str, Any]:
    project = event.get("project") if isinstance(event.get("project"), dict) else {}
    draft = event.get("draft") or event.get("messageSummary") or ""
    changed = "人工修改" in str(event.get("note") or "")
    return {
        "id": "",
        "acceptedAt": event.get("eventAt") or "",
        "url": event.get("sourceUrl") or event.get("url") or "",
        "changed": changed,
        "originalDraft": "",
        "editedDraft": draft,
        "lengthDelta": 0,
        "candidateName": event.get("candidateName") or "",
        "candidateTitle": event.get("candidateTitle") or "",
        "latestMessage": event.get("latestMessage") or "",
        "strategyKey": event.get("strategyKey") or "",
        "strategyLabel": event.get("strategyLabel") or "",
        "score": int(event.get("score") or 0),
        "grade": event.get("grade") or "",
        "project": {
            "client": project.get("client") or "",
            "position": project.get("position") or "",
            "confidence": project.get("confidence") or "采纳事件兜底",
            "rule": project.get("rule") or "",
        },
        "reasons": [],
        "missing": [],
        "risk": [],
        "fallbackSource": "reply_assistant_accept_event",
        "acceptEventId": event.get("id") or "",
    }


def merge_accept_event_samples(
    samples: list[dict[str, Any]],
    accept_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = list(samples)
    seen = {sample_dedupe_key(sample) for sample in merged}
    near_seen: dict[tuple[str, str, str, str], list[int]] = {}
    for sample in merged:
        epoch = accepted_epoch_ms(sample)
        if epoch is None:
            continue
        near_seen.setdefault(sample_near_dedupe_key(sample), []).append(epoch)
    for event in accept_events:
        sample = sample_from_accept_event(event)
        key = sample_dedupe_key(sample)
        if key in seen:
            continue
        epoch = accepted_epoch_ms(sample)
        near_key = sample_near_dedupe_key(sample)
        if epoch is not None and any(abs(epoch - other) <= 2000 for other in near_seen.get(near_key, [])):
            continue
        merged.append(sample)
        seen.add(key)
        if epoch is not None:
            near_seen.setdefault(near_key, []).append(epoch)
    return merged


def stable_sample_id(sample: dict[str, Any]) -> str:
    explicit = str(sample.get("id") or "").strip()
    if explicit:
        return explicit
    basis = {
        "acceptedAt": sample.get("acceptedAt"),
        "candidateName": sample.get("candidateName"),
        "editedDraft": sample.get("editedDraft"),
        "originalDraft": sample.get("originalDraft"),
        "strategyKey": sample.get("strategyKey"),
    }
    raw = json.dumps(basis, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def as_json(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False, sort_keys=True)


def normalize_row(sample: dict[str, Any], source: str) -> dict[str, Any]:
    project = sample.get("project") if isinstance(sample.get("project"), dict) else {}
    return {
        "sample_id": stable_sample_id(sample),
        "accepted_at": sample.get("acceptedAt") or "",
        "candidate_name": sample.get("candidateName") or "",
        "candidate_title": sample.get("candidateTitle") or "",
        "latest_message": sample.get("latestMessage") or "",
        "strategy_key": sample.get("strategyKey") or "",
        "strategy_label": sample.get("strategyLabel") or "",
        "score": int(sample.get("score") or 0),
        "grade": sample.get("grade") or "",
        "project_client": project.get("client") or "",
        "project_position": project.get("position") or "",
        "project_confidence": project.get("confidence") or "",
        "project_rule": project.get("rule") or "",
        "original_draft": sample.get("originalDraft") or "",
        "edited_draft": sample.get("editedDraft") or "",
        "changed": 1 if sample.get("changed") else 0,
        "length_delta": int(sample.get("lengthDelta") or 0),
        "reasons_json": as_json(sample.get("reasons") or []),
        "missing_json": as_json(sample.get("missing") or []),
        "risk_json": as_json(sample.get("risk") or []),
        "url": sample.get("url") or "",
        "raw_json": json.dumps(sample, ensure_ascii=False, sort_keys=True),
        "source": source,
    }


def existing_ids(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["sample_id"])
        for row in conn.execute("SELECT sample_id FROM reply_assistant_samples")
    }


def upsert_samples(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> dict[str, int]:
    before = existing_ids(conn)
    sql = """
    INSERT INTO reply_assistant_samples (
        sample_id, accepted_at, candidate_name, candidate_title, latest_message,
        strategy_key, strategy_label, score, grade, project_client, project_position,
        project_confidence, project_rule, original_draft, edited_draft, changed,
        length_delta, reasons_json, missing_json, risk_json, url, raw_json, source,
        synced_at
    ) VALUES (
        :sample_id, :accepted_at, :candidate_name, :candidate_title, :latest_message,
        :strategy_key, :strategy_label, :score, :grade, :project_client, :project_position,
        :project_confidence, :project_rule, :original_draft, :edited_draft, :changed,
        :length_delta, :reasons_json, :missing_json, :risk_json, :url, :raw_json, :source,
        datetime('now','localtime')
    )
    ON CONFLICT(sample_id) DO UPDATE SET
        accepted_at=excluded.accepted_at,
        candidate_name=excluded.candidate_name,
        candidate_title=excluded.candidate_title,
        latest_message=excluded.latest_message,
        strategy_key=excluded.strategy_key,
        strategy_label=excluded.strategy_label,
        score=excluded.score,
        grade=excluded.grade,
        project_client=excluded.project_client,
        project_position=excluded.project_position,
        project_confidence=excluded.project_confidence,
        project_rule=excluded.project_rule,
        original_draft=excluded.original_draft,
        edited_draft=excluded.edited_draft,
        changed=excluded.changed,
        length_delta=excluded.length_delta,
        reasons_json=excluded.reasons_json,
        missing_json=excluded.missing_json,
        risk_json=excluded.risk_json,
        url=excluded.url,
        raw_json=excluded.raw_json,
        source=excluded.source,
        synced_at=datetime('now','localtime')
    """
    for row in rows:
        conn.execute(sql, row)
    conn.commit()
    inserted = sum(1 for row in rows if row["sample_id"] not in before)
    return {"inserted": inserted, "updated": len(rows) - inserted}


def build_learning(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["暂未读取到采纳样本，后续需要先在插件里点“采纳修改”。"]

    changed_rows = [row for row in rows if row["changed"]]
    length_deltas = [row["length_delta"] for row in rows]
    shorter = sum(1 for row in rows if row["length_delta"] < 0)
    direct_company = sum(1 for row in rows if "是" in row["edited_draft"] and "岗位" in row["edited_draft"])
    question_light = sum(1 for row in rows if row["edited_draft"].count("？") + row["edited_draft"].count("?") <= 1)

    notes = [
        f"采纳样本 {len(rows)} 条，其中人工改写 {len(changed_rows)} 条，说明插件草稿已经能用，但仍需要学习你的删改偏好。",
        f"更短的话术 {shorter} 条，平均长度变化 {mean(length_deltas):.1f} 字；当前偏好明显是少铺垫、快确认。",
        f"{direct_company} 条改写直接点出岗位/客户方向，后续算法应优先保留“是什么机会”这句话。",
        f"{question_light} 条样本问题数不超过 1 个，符合“不要一下全问出去”的规则。",
    ]
    return notes


def write_report(
    output_dir: Path,
    source: str,
    rows: list[dict[str, Any]],
    stats: dict[str, int],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘回复助手采纳样本同步报告_{stamp}.md"

    strategies = Counter(row["strategy_key"] or "unknown" for row in rows)
    projects = Counter(
        f"{row['project_client'] or '未定客户'}/{row['project_position'] or '未定岗位'}"
        for row in rows
    )
    avg_score = mean([row["score"] for row in rows]) if rows else 0

    lines = [
        "# 猎聘回复助手采纳样本同步报告",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"数据来源：{source}",
        "",
        "## 同步结果",
        "",
        f"- 本次读取：{len(rows)} 条",
        f"- 新增入库：{stats.get('inserted', 0)} 条",
        f"- 更新覆盖：{stats.get('updated', 0)} 条",
        f"- 平均话术分：{avg_score:.1f}",
        "",
        "## 样本分布",
        "",
        "| 维度 | 分布 |",
        "|---|---|",
        f"| 话术场景 | {'、'.join(f'{k} {v}' for k, v in strategies.most_common()) or '暂无'} |",
        f"| 项目方向 | {'、'.join(f'{k} {v}' for k, v in projects.most_common(8)) or '暂无'} |",
        "",
        "## 这批样本给算法的信号",
        "",
    ]
    lines.extend(f"- {note}" for note in build_learning(rows))
    lines.extend(["", "## 最近样本", ""])
    for row in rows[:8]:
        edited = " ".join(row["edited_draft"].split())
        lines.append(
            f"- {row['candidate_name'] or '未识别'}｜{row['strategy_label'] or row['strategy_key'] or '未分类'}｜"
            f"{row['score']}分：{edited[:90]}{'...' if len(edited) > 90 else ''}"
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync accepted Liepin reply assistant samples.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR))
    parser.add_argument("--extension-id", default=DEFAULT_EXTENSION_ID)
    parser.add_argument("--json-file")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    samples, source = load_samples(args)
    rows = [normalize_row(sample, source) for sample in samples]

    conn = connect(db_path)
    try:
        ensure_schema(conn)
        stats = upsert_samples(conn, rows)
    finally:
        conn.close()

    report = write_report(output_dir, source, rows, stats)
    print(
        json.dumps(
            {
                "ok": True,
                "db": str(db_path),
                "source": source,
                "samples": len(rows),
                "inserted": stats.get("inserted", 0),
                "updated": stats.get("updated", 0),
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
