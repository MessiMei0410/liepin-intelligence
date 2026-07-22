#!/usr/bin/env python3
"""Generate Liepin follow-up talk drafts from learned talk algorithm rules."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


def connect_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def load_tasks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        WITH ci_best AS (
            SELECT *
            FROM (
                SELECT
                    ci.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY candidate_name, client, position
                        ORDER BY fit_score DESC, updated_at DESC, id DESC
                    ) AS rn
                FROM candidate_intelligence ci
            )
            WHERE rn = 1
        )
        SELECT
            t.id,
            t.source_id,
            ifnull(t.task_type, '') AS task_type,
            ifnull(t.lane_tag, '') AS lane_tag,
            ifnull(t.lane_reason, '') AS lane_reason,
            ifnull(t.candidate_name, '') AS candidate_name,
            ifnull(t.client, '') AS client,
            ifnull(t.position, '') AS position,
            ifnull(t.inferred_client, '') AS inferred_client,
            ifnull(t.inferred_position, '') AS inferred_position,
            ifnull(t.confirmed_client, '') AS confirmed_client,
            ifnull(t.confirmed_position, '') AS confirmed_position,
            ifnull(t.confirmation_status, '') AS confirmation_status,
            ifnull(t.match_confidence, 'unmatched') AS match_confidence,
            ifnull(r.intent, '') AS intent,
            ifnull(r.raw_text, '') AS raw_text,
            ifnull(r.candidate_title, '') AS candidate_title,
            ifnull(t.status, 'open') AS status,
            ifnull(ci.fit_score, 0) AS fit_score,
            ifnull(ci.fit_level, '') AS fit_level,
            ifnull(ci.strong_matches_json, '[]') AS strong_matches_json,
            ifnull(ci.weak_matches_json, '[]') AS weak_matches_json,
            ifnull(ci.verification_questions_json, '[]') AS verification_questions_json,
            ifnull(ci.recommendation_decision, '') AS recommendation_decision,
            ifnull(pp.pitch_points_json, '[]') AS pitch_points_json,
            ifnull(pp.risk_points_json, '[]') AS position_risk_points_json
        FROM followup_tasks t
        LEFT JOIN candidate_replies r
            ON t.source_table = 'candidate_replies' AND t.source_id = r.id
        LEFT JOIN ci_best ci
            ON t.candidate_name = ci.candidate_name
           AND ifnull(t.confirmed_client, ifnull(t.inferred_client, t.client)) = ci.client
           AND ifnull(t.confirmed_position, ifnull(t.inferred_position, t.position)) = ci.position
        LEFT JOIN position_profiles pp
            ON ifnull(t.confirmed_client, ifnull(t.inferred_client, t.client)) = pp.client
           AND ifnull(t.confirmed_position, ifnull(t.inferred_position, t.position)) = pp.position
        WHERE ifnull(t.status, 'open') = 'open'
        ORDER BY t.priority ASC, t.id ASC
        """
    ).fetchall()


def project_values(row: sqlite3.Row) -> tuple[str, str, str]:
    client = row["confirmed_client"] or row["inferred_client"] or row["client"]
    position = row["confirmed_position"] or row["inferred_position"] or row["position"]
    confidence = "confirmed" if row["confirmation_status"] == "confirmed" and client and position else row["match_confidence"]
    return client, position, confidence


def project_text(client: str, position: str) -> str:
    if client and position:
        return f"{client}的{position}"
    if position:
        return position
    if client:
        return f"{client}的岗位"
    return "这个机会"


def has_project_anchor(client: str, position: str) -> bool:
    return bool(client or position)


def clean(value: str) -> str:
    return " ".join(str(value or "").split())


def parse_json_list(text: str | None) -> list[str]:
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys([clean(item) for item in value if clean(item)]))


def first_item(items: list[str], default: str = "") -> str:
    return items[0] if items else default


def project_anchor(row: sqlite3.Row) -> str:
    strong = parse_json_list(row["strong_matches_json"])
    pitch = parse_json_list(row["pitch_points_json"])
    human_strong = [
        item for item in strong
        if not item.startswith("项目归属可信")
        and not item.startswith("候选人回复")
        and not item.startswith("当前分数")
    ]
    for item in human_strong:
        if item.startswith("能力标签重合："):
            return "您过往" + item.replace("能力标签重合：", "") + "相关经历"
        if item.startswith("履历摘要："):
            return item.replace("履历摘要：", "您的履历背景：")
        return item
    if strong:
        return "您的经历方向"
    if pitch:
        return pitch[0]
    return "您的经历方向"


def next_question(row: sqlite3.Row, fallback: str) -> str:
    questions = parse_json_list(row["verification_questions_json"])
    if questions:
        return questions[0].rstrip("。？?")
    return fallback.rstrip("。？?")


def one_key_question(row: sqlite3.Row, strategy: str) -> str:
    if strategy == "fast_lane_push":
        return "您今天方便约 10 分钟电话沟通吗"
    if strategy == "anchor_first_probe":
        return "您更关注岗位方向、客户背景，还是地点这块"
    if strategy == "asks_company":
        _, position, _ = project_values(row)
        if any(word in position for word in ("资深", "专家", "主管", "经理", "总监")):
            return "您这块大概有几年相关经验"
        return "您方便先说下这块大概几年经验"
    if strategy == "salary":
        return "您方便先说下目前大概总包区间"
    if strategy in {"positive_fit", "asks_if_open", "self_recommendation"}:
        return "您今天方便约 10 分钟电话沟通吗"
    if strategy == "location":
        return "这个工作地点您能接受吗"
    if strategy == "asks_direction":
        return "这个方向您方便继续了解吗"
    if strategy == "unclear_followup":
        return "您方便继续说下您最关注哪一块"
    return next_question(row, "您近期有在看新的机会吗")


def salutation(name: str) -> str:
    if not name:
        return "您好"
    if name.endswith(("先生", "女士", "老师")):
        return f"{name}，您好"
    return f"{name}您好"


def choose_strategy(row: sqlite3.Row) -> str:
    intent = row["intent"]
    raw = row["raw_text"]
    lane_tag = row["lane_tag"]
    short_confirmations = {"可以的", "好的", "嗯好", "ok", "OK"}
    if lane_tag == "fast_lane":
        return "fast_lane_push"
    if lane_tag == "anchor_first":
        return "anchor_first_probe"
    if intent == "short_confirmation" or row["task_type"] == "light_touch_followup" or raw.strip() in short_confirmations:
        return "light_confirmation"
    if "还在招" in raw or "还有在招" in raw or "目前还有" in raw:
        return "asks_if_open"
    if "负责三次电源" in raw or "负责什么方向" in raw or "主要方向" in raw or "产品方向" in raw or "做什么的" in raw or "哪一块业务" in raw or "做哪一块" in raw:
        return "asks_direction"
    if "您看下合适吗" in raw or "看下合适吗" in raw:
        return "self_recommendation"
    if intent == "location_concern" or "苏州机会" in raw or "上海以外" in raw or "区域" in raw or "工作地点" in raw:
        return "location"
    if intent == "need_more_info" or "哪家公司" in raw or "公司是哪" in raw or "工作年限" in raw or "这是哪家" in raw or "那个公司" in raw or "哪个公司" in raw or "我们有沟通过吗" in raw:
        return "asks_company"
    if intent == "need_contact" or "微信" in raw or "手机号" in raw or "简历" in raw:
        return "contact_exchange"
    if intent == "salary_concern" or "薪资" in raw or "可谈" in raw:
        return "salary"
    if intent == "not_interested" or "不对口" in raw or "不是" in raw or "不考虑" in raw:
        return "mismatch_or_reject"
    if intent == "interested" or "匹配" in raw or "感兴趣" in raw or "详聊" in raw or "应聘" in raw or "希望可以详聊" in raw:
        return "positive_fit"
    return "unclear_followup"


def draft_for(row: sqlite3.Row) -> tuple[str, str]:
    strategy = choose_strategy(row)
    prefix = salutation(row["candidate_name"])
    client, position, confidence = project_values(row)
    project = project_text(client, position)
    title_hint = f"看您目前是{row['candidate_title']}，" if row["candidate_title"] else ""
    anchor = project_anchor(row)
    fit_score = int(row["fit_score"] or 0)
    decision = row["recommendation_decision"] or ""
    question = one_key_question(row, strategy)

    if strategy == "fast_lane_push":
        if has_project_anchor(client, position):
            draft = (
                f"{prefix}，收到，您这边和{project}是能对上的。"
                f"{title_hint}{question}？我把岗位重点先和您快速对一下。"
            )
        else:
            draft = (
                f"{prefix}，收到，您这段背景我这边有兴趣继续往下推进。"
                f"{title_hint}{question}？我先把核心方向和您同步一下。"
            )
    elif strategy == "anchor_first_probe":
        if has_project_anchor(client, position):
            draft = (
                f"{prefix}，收到，目前我这边先按{project}和您沟通。"
                f"{question}？我先补一个您最关心的点。"
            )
        else:
            draft = (
                f"{prefix}，收到。"
                f"我先把这个机会的核心方向给您一句话说清，"
                f"{question}？"
            )
    elif strategy == "asks_company":
        if client:
            draft = (
                f"{prefix}，可以的，目前沟通的是{project}。"
                f"{title_hint}{question}？"
            )
        else:
            draft = (
                f"{prefix}，收到。客户名称我这边先确认下可透露范围，岗位方向是{project}。"
                f"{title_hint}{question}？"
            )
    elif strategy == "asks_if_open":
        draft = (
            f"{prefix}，还在招的，目前看的是{project}。"
            f"{title_hint}{question}？"
        )
    elif strategy == "asks_direction":
        draft = (
            f"{prefix}，是的，目前主要按{project}方向沟通。"
            f"{question}？"
        )
    elif strategy == "contact_exchange":
        if has_project_anchor(client, position):
            draft = f"{prefix}，我加您。我这边是{project}在看，微信里先把岗位要点发您。"
        else:
            draft = f"{prefix}，我加您。我这边主要看半导体和高端制造方向机会，微信里再和您同步。"
    elif strategy == "salary":
        draft = (
            f"{prefix}，收到，薪资这块可以先对齐。"
            f"{'我先按' + project + '判断预算匹配度。' if has_project_anchor(client, position) else ''}"
            f"{question}？"
        )
    elif strategy == "mismatch_or_reject":
        draft = (
            "明白，有合适机会随时沟通。"
        )
    elif strategy == "light_confirmation":
        if has_project_anchor(client, position):
            draft = (
                f"{prefix}，收到，我这边先把{project}的关键信息和方向发您。"
                f"{question}？"
            )
        else:
            draft = (
                f"{prefix}，收到，我先把这个机会的核心方向和要求发您。"
                "您看完后如果合适，我们再继续往下沟通。"
            )
    elif strategy == "location":
        draft = (
            f"{prefix}，这个机会工作地点是按{project}来沟通的。"
            f"{question}？"
        )
    elif strategy == "self_recommendation":
        draft = (
            f"{prefix}，收到，我看您这段背景和{project}有可聊空间。"
            f"{question}？我先把岗位重点和您经历快速对一下。"
        )
    elif strategy == "positive_fit":
        draft = (
            f"{prefix}，好的，我看您和{project}方向是匹配的。"
            f"{title_hint}{question}？我先把岗位重点和您这边经历快速对一下。"
        )
    elif strategy == "unclear_followup":
        if client or position:
            draft = (
                f"{prefix}，收到，我这边目前沟通的是{project}。"
                f"{question}？"
            )
        else:
            draft = (
                f"{prefix}，收到。"
                f"{question}？"
            )
    else:
        draft = (
            f"{prefix}，我这边主要看半导体和高端制造方向机会。"
            "方便的话咱们先加个微信，后面有贴近您背景的岗位我及时同步。"
        )

    return strategy, draft


def evaluate_draft(row: sqlite3.Row, strategy: str, draft: str) -> dict:
    client, position, confidence = project_values(row)
    missing: list[str] = []
    risk: list[str] = []
    score = 72

    if confidence in ("confirmed", "high"):
        score += 12
    elif confidence == "medium":
        score += 6
    elif confidence == "low":
        score -= 8
        missing.append("客户/岗位需确认")
    else:
        score -= 16
        missing.append("客户/岗位未确认")

    if not client:
        score -= 6
        missing.append("客户名")
    if not position:
        score -= 6
        missing.append("岗位名")
    if not row["candidate_title"]:
        score -= 4
        missing.append("候选人头衔")
    if not int(row["fit_score"] or 0):
        score -= 4
        missing.append("匹配评分")
    if not parse_json_list(row["strong_matches_json"]):
        score -= 3
        missing.append("强匹配点")
    if parse_json_list(row["position_risk_points_json"]):
        risk.append("岗位仍有待补信息：" + "、".join(parse_json_list(row["position_risk_points_json"])[:2]))

    if strategy in ("positive_fit", "contact_exchange", "salary", "self_recommendation") and "薪资" in draft:
        score += 5
    if "地点" in draft:
        score += 4
    if "10 分钟" in draft or "微信" in draft:
        score += 4
    if strategy in {"asks_if_open", "asks_direction", "self_recommendation", "location", "fast_lane_push"}:
        score += 6
    if strategy == "anchor_first_probe":
        score += 2
    if len(draft) > 160:
        score -= 6
        risk.append("略长，复制前可压缩")
    if draft.count("？") + draft.count("?") > 1:
        score -= 8
        risk.append("问题偏多，建议压到一个主问题")
    if project_text(client, position) == "这个机会":
        risk.append("项目不明确")
    if strategy == "mismatch_or_reject":
        score = max(score, 78)
    if strategy in {"asks_company", "asks_if_open", "asks_direction"} and not client:
        risk.append("候选人已问公司，但客户名未确认")
    if strategy == "contact_exchange" and confidence in ("low", "unmatched", ""):
        risk.append("适合先转联系方式，但要避免过度承诺岗位")
    if strategy == "anchor_first_probe" and not has_project_anchor(client, position):
        risk.append("项目锚点仍弱，发送前最好补一句岗位方向")

    score = max(35, min(96, score))
    reason = f"策略={strategy}；项目置信={confidence or 'unmatched'}；包含下一步动作={'是' if ('10 分钟' in draft or '微信' in draft) else '否'}。"
    return {
        "score": score,
        "reason": reason,
        "risk": "；".join(risk) if risk else "无明显风险",
        "missing": "、".join(dict.fromkeys(missing)) if missing else "无",
    }


def update_drafts(conn: sqlite3.Connection, rows: list[sqlite3.Row], dry_run: bool) -> list[dict]:
    results: list[dict] = []
    for row in rows:
        strategy, draft = draft_for(row)
        client, position, confidence = project_values(row)
        quality = evaluate_draft(row, strategy, draft)
        results.append(
            {
                "task_id": row["id"],
                "reply_id": row["source_id"],
                "candidate_name": row["candidate_name"],
                "strategy": strategy,
                "project": project_text(client, position),
                "confidence": confidence,
                **quality,
                "draft": draft,
            }
        )
        if dry_run:
            continue
        conn.execute(
            """
            UPDATE followup_tasks
            SET draft_message = ?,
                talk_strategy = ?,
                talk_score = ?,
                talk_reason = ?,
                talk_risk = ?,
                talk_missing = ?,
                talk_generated_at = datetime('now','localtime'),
                updated_at = datetime('now','localtime')
            WHERE id = ?
            """,
            (draft, strategy, quality["score"], quality["reason"], quality["risk"], quality["missing"], row["id"]),
        )
        if row["source_id"]:
            conn.execute(
                """
                UPDATE candidate_replies
                SET draft_message = ?,
                    talk_strategy = ?,
                    talk_score = ?,
                    talk_reason = ?,
                    talk_risk = ?,
                    talk_missing = ?,
                    talk_generated_at = datetime('now','localtime')
                WHERE id = ?
                """,
                (draft, strategy, quality["score"], quality["reason"], quality["risk"], quality["missing"], row["source_id"]),
            )
        conn.execute(
            """
            INSERT INTO talk_draft_audits
                (task_id, reply_id, candidate_name, strategy, score, reason, risk, missing, draft)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["source_id"],
                row["candidate_name"],
                strategy,
                quality["score"],
                quality["reason"],
                quality["risk"],
                quality["missing"],
                draft,
            ),
        )
    if not dry_run:
        conn.commit()
    return results


def write_report(results: list[dict], output_dir: Path, dry_run: bool) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "dryrun" if dry_run else "applied"
    path = output_dir / f"猎聘话术算法草稿_{suffix}_{stamp}.md"
    counts = Counter(row["strategy"] for row in results)
    lines = [
        "# 猎聘话术算法草稿",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"模式：{'预览' if dry_run else '已写回本地草稿'}",
        "",
        f"- 生成草稿：{len(results)}",
        f"- 策略分布：{'、'.join(f'{k} {v}' for k, v in sorted(counts.items()))}",
        "",
        "| 待办 | 候选人 | 策略 | 分数 | 项目 | 置信 | 风险 | 缺失 | 草稿 |",
        "|---:|---|---|---:|---|---|---|---|---|",
    ]
    for row in results:
        lines.append(
            "| {task_id} | {name} | {strategy} | {score} | {project} | {confidence} | {risk} | {missing} | {draft} |".format(
                task_id=row["task_id"],
                name=(row["candidate_name"] or "未识别").replace("|", "｜"),
                strategy=row["strategy"],
                score=row["score"],
                project=row["project"].replace("|", "｜"),
                confidence=row["confidence"],
                risk=row["risk"].replace("|", "｜"),
                missing=row["missing"].replace("|", "｜"),
                draft=row["draft"].replace("|", "｜"),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate local follow-up drafts from learned talk algorithm.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = connect_db(db_path)
    try:
        rows = load_tasks(conn)
        results = update_drafts(conn, rows, args.dry_run)
    finally:
        conn.close()
    report = write_report(results, output_dir, args.dry_run)
    print(
        json.dumps(
            {
                "ok": True,
                "dry_run": args.dry_run,
                "drafts": len(results),
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
