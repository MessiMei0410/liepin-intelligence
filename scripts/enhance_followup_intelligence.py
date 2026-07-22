#!/usr/bin/env python3
"""Enhance Liepin reply follow-ups with conservative context and draft replies."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


REPLY_COLUMNS = {
    "candidate_title": "TEXT",
    "inferred_client": "TEXT",
    "inferred_position": "TEXT",
    "match_confidence": "TEXT DEFAULT 'unmatched'",
    "match_reason": "TEXT",
    "draft_message": "TEXT",
}

TASK_COLUMNS = {
    "inferred_client": "TEXT",
    "inferred_position": "TEXT",
    "match_confidence": "TEXT DEFAULT 'unmatched'",
    "match_reason": "TEXT",
    "draft_message": "TEXT",
}

ACTIVE_STATUSES = {"recommended", "contacted", "interviewing", "offered", "greeted", "replied"}

ROLE_KEYWORDS = (
    ("高级机械结构工程师", 12),
    ("机械结构", 10),
    ("结构工程师", 9),
    ("机械工程师", 10),
    ("机械设备", 8),
    ("solidworks", 6),
    ("ansys", 6),
    ("机械", 3),
    ("fpga", 12),
    ("hardware development", 9),
    ("硬件开发", 8),
    ("电力电子", 10),
    ("电源", 8),
    ("acdc", 12),
    ("device专家", 12),
    ("device", 8),
    ("器件", 6),
    ("技术应用总监", 10),
    ("应用总监", 8),
    ("产品总监", 8),
    ("产品经理", 6),
    ("项目经理", 5),
    ("研发经理", 6),
    ("部门经理", 5),
    ("模块化", 6),
    ("cim", 10),
    ("mes", 10),
    ("amhs", 10),
    ("pvd", 9),
    ("cvd", 9),
    ("量测", 7),
    ("工艺", 4),
    ("软件", 5),
    ("team leader", 5),
    ("leader", 4),
)


def connect_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_columns(conn: sqlite3.Connection) -> list[str]:
    added: list[str] = []
    reply_existing = table_columns(conn, "candidate_replies")
    for name, spec in REPLY_COLUMNS.items():
        if name not in reply_existing:
            conn.execute(f"ALTER TABLE candidate_replies ADD COLUMN {name} {spec}")
            added.append(f"candidate_replies.{name}")

    task_existing = table_columns(conn, "followup_tasks")
    for name, spec in TASK_COLUMNS.items():
        if name not in task_existing:
            conn.execute(f"ALTER TABLE followup_tasks ADD COLUMN {name} {spec}")
            added.append(f"followup_tasks.{name}")
    conn.commit()
    return added


def has_confirmed_project(reply: sqlite3.Row) -> bool:
    keys = set(reply.keys())
    return (
        "confirmation_status" in keys
        and reply["confirmation_status"] == "confirmed"
        and bool(reply["confirmed_client"])
        and bool(reply["confirmed_position"])
    )


def latest_ingest_json(output_dir: Path) -> Path | None:
    files = sorted(output_dir.glob("liepin_im_replies_ingest_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def load_liepin_rows(path: Path | None) -> dict[str, dict]:
    if not path or not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("data", {}).get("rows", [])
    mapping: dict[str, dict] = {}
    for row in rows:
        cid = row.get("conversation_id")
        if cid:
            mapping[cid] = row
    return mapping


def clean_name(name: str) -> str:
    return (name or "").replace("先生", "").replace("女士", "").replace("老师", "").strip()


def is_generic_name(name: str) -> bool:
    stripped = clean_name(name)
    return len(stripped) <= 1 or (name or "").endswith(("先生", "女士", "老师"))


def row_value(row: sqlite3.Row | dict, key: str) -> str:
    try:
        return str(row[key] or "")
    except (KeyError, IndexError, TypeError):
        return ""


def compact_text(text: str) -> str:
    return re.sub(r"[\s/_\-·,，。:：;；()（）\[\]【】]+", "", (text or "").lower())


def contains_term(text: str, term: str) -> bool:
    needle = compact_text(term)
    return len(needle) >= 2 and needle in compact_text(text)


def candidate_text(row: sqlite3.Row | dict) -> str:
    return " ".join(
        row_value(row, key)
        for key in ("name", "title", "position", "company", "skills", "notes", "client")
    )


def name_match_kind(input_name: str, candidate_name: str) -> str:
    base = clean_name(input_name)
    if not base or not candidate_name:
        return "none"
    if candidate_name == input_name or candidate_name == base:
        return "exact"
    if not is_generic_name(input_name) and len(base) >= 2 and candidate_name.startswith(base):
        return "prefix"
    if candidate_name.startswith(base[:1]):
        return "surname"
    return "none"


def title_score(source_text: str, row: sqlite3.Row | dict, client_hint: str = "", position_hint: str = "") -> tuple[int, list[str]]:
    hay = candidate_text(row)
    score = 0
    reasons: list[str] = []

    for keyword, weight in ROLE_KEYWORDS:
        if contains_term(source_text, keyword) and contains_term(hay, keyword):
            score += weight
            reasons.append(keyword)

    for token in re.findall(r"[a-zA-Z][a-zA-Z0-9+#.]{1,}", source_text or ""):
        token_lower = token.lower()
        if len(token_lower) >= 3 and token_lower in hay.lower():
            score += 2
            reasons.append(token_lower)

    candidate_position = row_value(row, "position")
    candidate_title = row_value(row, "title")
    candidate_client = row_value(row, "client")
    if candidate_position and contains_term(source_text, candidate_position):
        score += 10
        reasons.append(f"原话含岗位:{candidate_position}")
    if position_hint and candidate_position and (
        contains_term(candidate_position, position_hint) or contains_term(position_hint, candidate_position)
    ):
        score += 10
        reasons.append(f"岗位线索一致:{position_hint}")
    if candidate_title and len(compact_text(candidate_title)) >= 4 and contains_term(source_text, candidate_title):
        score += 8
        reasons.append(f"头衔一致:{candidate_title}")
    if client_hint and candidate_client and client_hint == candidate_client:
        score += 4
        reasons.append(f"客户线索一致:{client_hint}")
    if row_value(row, "status") in ACTIVE_STATUSES:
        score += 2
        reasons.append("状态活跃")

    return score, reasons


def find_candidate_match(
    conn: sqlite3.Connection,
    name: str,
    title: str,
    raw_text: str = "",
    client_hint: str = "",
    position_hint: str = "",
) -> dict | None:
    base = clean_name(name)
    if not base:
        return None
    surname = base[:1]
    if is_generic_name(name):
        rows = conn.execute(
            """
            SELECT *
            FROM candidates
            WHERE name LIKE ?
            ORDER BY
              CASE WHEN status IN ('recommended','contacted','interviewing','offered','greeted','replied') THEN 0 ELSE 1 END,
              updated_at DESC,
              created_at DESC
            LIMIT 80
            """,
            (base + "%",),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT *
            FROM candidates
            WHERE name = ?
               OR name LIKE ?
               OR name LIKE ?
            ORDER BY
              CASE WHEN status IN ('recommended','contacted','interviewing','offered','greeted','replied') THEN 0 ELSE 1 END,
              updated_at DESC,
              created_at DESC
            LIMIT 80
            """,
            (name, base + "%", surname + "%"),
        ).fetchall()
    if not rows:
        return None

    source_text = f"{title} {raw_text}"
    scored: list[dict] = []
    for row in rows:
        kind = name_match_kind(name, row["name"] or "")
        if kind == "none":
            continue
        name_score = {"exact": 50, "prefix": 25, "surname": 2}[kind]
        role_score, role_reasons = title_score(source_text, row, client_hint, position_hint)
        score = name_score + role_score
        scored.append({
            "row": row,
            "score": score,
            "name_kind": kind,
            "role_score": role_score,
            "reasons": role_reasons,
        })
    if not scored:
        return None

    scored.sort(key=lambda item: item["score"], reverse=True)
    top = scored[0]
    second_score = scored[1]["score"] if len(scored) > 1 else -1

    if top["name_kind"] == "exact":
        confidence = "high"
    elif top["name_kind"] == "prefix" and top["score"] >= 28 and top["score"] - second_score >= 5:
        confidence = "high" if top["score"] >= 36 else "medium"
    elif top["name_kind"] == "surname" and top["role_score"] >= 10 and top["score"] >= 14 and top["score"] - second_score >= 5:
        confidence = "medium"
    else:
        return None

    reason_bits = [
        f"姓名匹配={top['name_kind']}",
        f"匹配分={top['score']}",
    ]
    if top["reasons"]:
        reason_bits.append("命中线索：" + "、".join(dict.fromkeys(top["reasons"][:6])))
    return {"row": top["row"], "confidence": confidence, "reason": "；".join(reason_bits)}


def find_candidate(conn: sqlite3.Connection, name: str, title: str, raw_text: str = "") -> sqlite3.Row | None:
    match = find_candidate_match(conn, name, title, raw_text)
    return match["row"] if match else None


def infer_project_from_text(conn: sqlite3.Connection, raw: str, title: str) -> dict:
    text = f"{raw} {title}".lower()

    if "device专家" in text or "device 专家" in text:
        return {
            "candidate_id": None,
            "candidate_company": "",
            "client": "鹏新旭",
            "position": "Device专家",
            "confidence": "high" if position_exists(conn, "鹏新旭", "Device专家") else "medium",
            "reason": "候选人原话直接提到 Device专家，岗位表存在对应职位。",
        }

    if "资深机械工程师" in raw or "机械工程师职位" in raw:
        return {
            "candidate_id": None,
            "candidate_company": "",
            "client": "微导纳米",
            "position": "机械工程师",
            "confidence": "high" if position_exists(conn, "微导纳米", "机械工程师") else "medium",
            "reason": "候选人原话提到资深机械工程师职位，当前岗位表有微导纳米机械工程师。",
        }

    if "fpga" in text or "fpag" in text:
        return {
            "candidate_id": None,
            "candidate_company": "",
            "client": "",
            "position": "FPGA相关岗位",
            "confidence": "low",
            "reason": "回复或头衔含 FPGA，但当前岗位表未找到确定客户/岗位，不自动挂库。",
        }

    if "acdc服务器电源研发总监" in raw.lower():
        return {
            "candidate_id": None,
            "candidate_company": "",
            "client": "",
            "position": "ACDC服务器电源研发总监",
            "confidence": "medium",
            "reason": "候选人原话直接提到 ACDC服务器电源研发总监，但岗位表未找到完全对应客户。",
        }

    if "电力电子" in text or "hardware development" in text or "硬件开发" in text:
        return {
            "candidate_id": None,
            "candidate_company": "",
            "client": "",
            "position": "硬件/电力电子研发相关岗位",
            "confidence": "low",
            "reason": "回复或头衔含硬件、电力电子方向，但缺少明确客户和岗位名称。",
        }

    if "电源" in raw or "电源" in title:
        return {
            "candidate_id": None,
            "candidate_company": "",
            "client": "",
            "position": "电源研发相关岗位",
            "confidence": "low",
            "reason": "候选人回复或头衔含电源方向，但缺少明确客户和岗位名称。",
        }

    return {
        "candidate_id": None,
        "candidate_company": "",
        "client": "",
        "position": "",
        "confidence": "unmatched",
        "reason": "当前列表信息不足，保留人工确认。",
    }


def should_create_reply_candidate(name: str) -> bool:
    return bool(clean_name(name)) and not is_generic_name(name) and len(clean_name(name)) >= 2


def create_reply_candidate(conn: sqlite3.Connection, reply: sqlite3.Row, title: str, context: dict) -> sqlite3.Row | None:
    name = reply["candidate_name"] or ""
    if not should_create_reply_candidate(name):
        return None
    conversation_id = reply["conversation_id"] or ""
    existing = conn.execute(
        """
        SELECT *
        FROM candidates
        WHERE source = 'liepin_im'
          AND name = ?
          AND notes LIKE ?
        LIMIT 1
        """,
        (name, f"%{conversation_id}%"),
    ).fetchone()
    if existing:
        return existing

    company = "猎聘职聊（公司待确认）"
    now = datetime.now().isoformat(timespec="seconds")
    notes = (
        "从猎聘职聊回复自动建档；"
        f"conversation_id={conversation_id}；"
        f"原始头衔={title or '未识别'}；"
        f"原始回复={reply['raw_text'] or ''}"
    )
    conn.execute(
        """
        INSERT INTO candidates
          (name, company, title, client, position, status, notes, source, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'replied', ?, 'liepin_im', ?, ?)
        """,
        (
            name,
            company,
            title,
            context.get("client") or "",
            context.get("position") or "",
            notes,
            now,
            now,
        ),
    )
    return conn.execute("SELECT * FROM candidates WHERE id = last_insert_rowid()").fetchone()


def candidate_context(candidate: sqlite3.Row, confidence: str, reason: str) -> dict:
    return {
        "candidate_id": candidate["id"],
        "candidate_company": candidate["company"] or "",
        "client": candidate["client"] or "",
        "position": candidate["position"] or "",
        "confidence": confidence,
        "reason": reason,
    }


def position_exists(conn: sqlite3.Connection, client: str, title: str) -> bool:
    if not client or not title:
        return False
    row = conn.execute("SELECT 1 FROM positions WHERE client = ? AND title = ? LIMIT 1", (client, title)).fetchone()
    return row is not None


def infer_context(conn: sqlite3.Connection, reply: sqlite3.Row, liepin_row: dict | None) -> dict:
    raw = reply["raw_text"] or ""
    title = (liepin_row or {}).get("title", "") or reply["candidate_title"] or ""
    project = infer_project_from_text(conn, raw, title)

    match = find_candidate_match(
        conn,
        reply["candidate_name"] or "",
        title,
        raw,
        client_hint=project.get("client", ""),
        position_hint=project.get("position", ""),
    )
    if match:
        candidate = match["row"]
        return {
            "candidate_id": candidate["id"],
            "candidate_company": candidate["company"] or "",
            "client": candidate["client"] or project.get("client", ""),
            "position": candidate["position"] or project.get("position", ""),
            "confidence": match["confidence"],
            "reason": f"人才库候选人匹配；{match['reason']}。",
        }

    created = create_reply_candidate(conn, reply, title, project)
    if created:
        return {
            "candidate_id": created["id"],
            "candidate_company": created["company"] or "",
            "client": created["client"] or "",
            "position": created["position"] or "",
            "confidence": "created",
            "reason": f"完整姓名未在历史人才库唯一命中，已按猎聘职聊回复自动建档；{project['reason']}",
        }

    return project


def display_project(client: str, position: str) -> str:
    if client and position:
        return f"{client}的{position}"
    if position:
        return position
    if client:
        return f"{client}的岗位"
    return "这个岗位"


def draft_message(name: str, intent: str, client: str, position: str, raw: str) -> str:
    salutation = name if name else "您好"
    if salutation.endswith(("先生", "女士", "老师")):
        prefix = f"{salutation}，您好"
    else:
        prefix = f"{salutation}您好"
    project = display_project(client, position)

    if intent == "interested":
        return (
            f"{prefix}，感谢您关注。您提到和{project}比较匹配，我先和您确认几个关键信息："
            "目前主要看您的相关项目经验、当前薪资/期望区间、地点接受度和近期看机会的动机。"
            "您今天方便约 10 分钟电话沟通一下吗？我确认后再把更完整的信息同步给您。"
        )
    if intent == "need_more_info":
        return (
            f"{prefix}，收到。我先补充一下：目前沟通的是{project}。"
            "我这边可以先和您对一下岗位核心要求、工作年限、地点和薪资范围，"
            "再判断是否值得继续深入。您方便的话，我把几个关键点先发您确认。"
        )
    if intent == "need_contact":
        return (
            f"{prefix}，可以的。为了后续沟通更方便，我们可以加微信。"
            "我也先简单确认一下您当前主要负责的方向、近期看机会的意愿，以及对地点和薪资的基本要求。"
        )
    if intent == "salary_concern":
        return (
            f"{prefix}，收到，薪资这块我们可以先对齐。"
            "方便说下您目前总包、固定和奖金结构，以及期望区间吗？"
            "我确认客户预算后，再给您一个更明确的判断。"
        )
    if intent == "location_concern":
        return (
            f"{prefix}，理解，地点确实需要提前确认。"
            "您目前主要考虑哪些城市？如果岗位方向匹配，通勤或搬迁上有哪些不能接受的条件？"
        )
    if intent == "not_interested":
        return (
            f"{prefix}，收到，感谢您直接反馈。"
            "我先记录为方向不匹配，后面如果有更贴近您专业背景和职业方向的机会，再和您沟通。"
        )
    return f"{prefix}，收到。我先看下您这条回复对应的岗位信息，再给您更准确的反馈。"


def enhance(conn: sqlite3.Connection, liepin_rows: dict[str, dict]) -> dict:
    replies = conn.execute("SELECT * FROM candidate_replies ORDER BY id").fetchall()
    updated = 0
    confidence_counts: dict[str, int] = {}
    rows_for_report = []

    for reply in replies:
        liepin_row = liepin_rows.get(reply["conversation_id"])
        title = (liepin_row or {}).get("title", "") or reply["candidate_title"] or ""
        context = infer_context(conn, reply, liepin_row)
        confirmed = has_confirmed_project(reply)
        client = reply["confirmed_client"] if confirmed else (context["client"] or reply["client"] or "")
        position = reply["confirmed_position"] if confirmed else (context["position"] or reply["position"] or "")
        confidence = "confirmed" if confirmed else context["confidence"]
        reason = "人工确认项目，自动增强仅更新话术。" if confirmed else context["reason"]
        draft = draft_message(reply["candidate_name"] or "", reply["intent"] or "unclear", client, position, reply["raw_text"] or "")
        confidence_counts[confidence] = confidence_counts.get(confidence, 0) + 1

        conn.execute(
            """
            UPDATE candidate_replies
            SET candidate_id = coalesce(candidate_id, ?),
                candidate_company = coalesce(nullif(candidate_company, ''), ?),
                candidate_title = ?,
                inferred_client = ?,
                inferred_position = ?,
                match_confidence = ?,
                match_reason = ?,
                draft_message = ?
            WHERE id = ?
            """,
            (
                context["candidate_id"],
                context["candidate_company"],
                title,
                reply["inferred_client"] if confirmed else context["client"],
                reply["inferred_position"] if confirmed else context["position"],
                confidence,
                reason,
                draft,
                reply["id"],
            ),
        )
        conn.execute(
            """
            UPDATE followup_tasks
            SET candidate_id = coalesce(candidate_id, ?),
                candidate_company = coalesce(nullif(candidate_company, ''), ?),
                inferred_client = ?,
                inferred_position = ?,
                match_confidence = ?,
                match_reason = ?,
                draft_message = ?
            WHERE source_table = 'candidate_replies' AND source_id = ?
            """,
            (
                context["candidate_id"],
                context["candidate_company"],
                reply["inferred_client"] if confirmed else context["client"],
                reply["inferred_position"] if confirmed else context["position"],
                confidence,
                reason,
                draft,
                reply["id"],
            ),
        )
        updated += 1
        rows_for_report.append({
            "id": reply["id"],
            "name": reply["candidate_name"] or "",
            "title": title,
            "intent": reply["intent"] or "",
            "client": client,
            "position": position,
            "confidence": confidence,
            "reason": reason,
            "raw_text": reply["raw_text"] or "",
            "draft_message": draft,
        })

    conn.commit()
    return {
        "updated": updated,
        "confidence_counts": confidence_counts,
        "rows": rows_for_report,
    }


def write_report(result: dict, output_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘回复智能增强报告_{stamp}.md"
    counts = result["confidence_counts"]
    lines = [
        "# 猎聘回复智能增强报告",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 总览",
        "",
        f"- 已增强回复：{result['updated']}",
        f"- 高置信匹配：{counts.get('high', 0)}",
        f"- 中置信匹配：{counts.get('medium', 0)}",
        f"- 低置信线索：{counts.get('low', 0)}",
        f"- 自动建档：{counts.get('created', 0)}",
        f"- 仍需人工确认：{counts.get('unmatched', 0)}",
        "",
        "## 明细",
        "",
        "| ID | 候选人 | 头衔 | 意图 | 推断项目 | 置信度 | 原因 | 话术草稿 |",
        "|---:|---|---|---|---|---|---|---|",
    ]
    for row in result["rows"]:
        project = display_project(row["client"], row["position"])
        lines.append(
            "| {id} | {name} | {title} | {intent} | {project} | {confidence} | {reason} | {draft} |".format(
                id=row["id"],
                name=(row["name"] or "未识别").replace("|", "｜"),
                title=(row["title"] or "").replace("|", "｜"),
                intent=(row["intent"] or "").replace("|", "｜"),
                project=project.replace("|", "｜"),
                confidence=row["confidence"],
                reason=row["reason"].replace("|", "｜"),
                draft=row["draft_message"].replace("|", "｜"),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Enhance candidate follow-up tasks with inferred context and draft replies.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--liepin-json", default="")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    liepin_json = Path(args.liepin_json).expanduser() if args.liepin_json else latest_ingest_json(output_dir)

    conn = connect_db(db_path)
    try:
        added_columns = ensure_columns(conn)
        liepin_rows = load_liepin_rows(liepin_json)
        result = enhance(conn, liepin_rows)
    finally:
        conn.close()

    report = write_report(result, output_dir)
    summary = {
        "ok": True,
        "db": str(db_path),
        "liepin_json": str(liepin_json) if liepin_json else "",
        "added_columns": added_columns,
        "updated": result["updated"],
        "confidence_counts": result["confidence_counts"],
        "report": str(report),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
