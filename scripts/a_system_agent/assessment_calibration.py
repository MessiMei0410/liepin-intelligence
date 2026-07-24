"""S6-4：评估校准学习闭环 —— 改判样例库 / 校准注入 / 采纳率度量 / 校准报告。

口径来源：docs/TASKCARD_S6-4_评估校准闭环_20260724.md + PRD §3/§5（S6-4 行）。

四件事：
1. 改判样例库（assessment_calibration_samples 表）：advisor_action ∈ {modified, rejected}
   的 assessment 自动进入校准集，结构为 {维度, 机器原判, 顾问改判(advisor_note), 客户,
   岗位类型, as_of}。只存改判内容与维度标签，不存简历原文、不存人选身份。
2. 校准注入：生成新评估时检索同客户或同岗位类型最近 ≤5 条改判样例，作为 few-shot
   上下文注入三次 LLM 调用的 payload（键名 calibration，无样例时该键整个不出现）。
   注入内容只进 prompt，不落 artifact 正文/markdown/推荐报告，UI 永不渲染样例内容。
3. 采纳率度量：compute_metrics 按维度×客户聚合采纳/改判/否决率，数据不足（<MIN_N）
   的分组三个率一律如实 null；totals 与库内 advisor_action 实际分布严格一致。
4. 校准报告：generate_report 生成 markdown 到 work/calibration/（gitignore 已排除
   work/，不进 git），周度手动触发，不做定时。

红线（写死）：
- 样例只影响判断口径，不放宽证据强约束（verdict 仍必须挂证据、标置信度）——该约束
  同时写进注入 instruction；
- 注入内容不得出现在 UI/推荐报告对外文本（"顾问上次改判过"这种话永不外泄）——
  样例 note/verdict 不进 artifact doc，doc 只记 samples_injected 计数与 sample_ids；
- 敏感属性红线不变：改判样例的 note 或机器原判命中年龄/性别/婚育/户籍词表 →
  拒绝入库（不抛错阻断顾问动作写回，记 blocked 扫描日志，契约测试锚定）；
- 文案业务语言：calibration → 「评估校准」，采纳率 → 「顾问点头率」。
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

TABLE = "assessment_calibration_samples"

# 进入校准集的顾问动作（采纳不算改判，pending 未决策）
SAMPLE_ACTIONS = ("modified", "rejected")

# 校准注入上限（任务卡：最近 3-5 条；5 为硬上限，契约测试锚定）
INJECT_LIMIT = 5

# 采纳率度量：分组样本量 < MIN_N 时三个率如实返回 null（数据不足不硬算）
METRICS_MIN_N = 3

# 校准报告窗口（周度）
REPORT_WINDOW_DAYS = 7

# 维度标签（与 candidate_assessment.DIMENSIONS_IMPLEMENTED 对齐；overall=整份评估口径）
METRIC_DIMENSIONS = ("trajectory", "move_history", "percentile", "motivation", "risks")
DIMENSION_LABELS = {
    "trajectory": "职业轨迹",
    "move_history": "跳槽质量史",
    "percentile": "在同龄人里的位置",
    "motivation": "动机与时机",
    "risks": "需要核实的问题",
    "overall": "整体口径",
}

# 维度打标词表：改判 note 命中即把该样例挂到对应维度（可多挂）；都不命中 → overall。
_DIMENSION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "trajectory": ("轨迹", "晋升", "技术栈", "含金量", "平台"),
    "move_history": ("跳槽", "平移", "上升", "下行", "这单"),
    "percentile": ("分位", "同龄人", "落位", "前 10", "前 25", "中位", "水平"),
    "motivation": ("动机", "时机", "变动信号", "动的可能", "诉求"),
    "risks": ("风险", "核实", "空窗", "gap", "GAP", "包装", "通胀", "学历"),
}

# 注入 instruction：红线随样例一起进 prompt（不放宽证据 + 永不外泄 + 不推翻简历事实）
CALIBRATION_INSTRUCTION = (
    "以下为本工作台顾问的历史改判口径样例（已脱敏，不含人选身份与简历原文），仅用于校准判断口径——"
    "了解该顾问/该客户在同类岗位上看重什么、接受什么（如接受平移、不看重某项硬指标等）。"
    "约束：1) 不得因此放宽证据要求，每条 verdict 仍必须挂证据，拿不准必须标 inferred；"
    "2) 严禁在 verdict、consultant_summary 或任何输出文本中提及这些样例或「顾问改判」本身；"
    "3) 样例只影响口径倾向，不得推翻简历事实；与简历事实冲突时一律以简历为准。"
)

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS assessment_calibration_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL UNIQUE,
    candidate_id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    client TEXT NOT NULL DEFAULT '',
    job_type TEXT NOT NULL DEFAULT '',
    advisor_action TEXT NOT NULL,
    dimensions_json TEXT NOT NULL DEFAULT '[]',
    machine_verdicts_json TEXT NOT NULL DEFAULT '{}',
    advisor_note TEXT NOT NULL DEFAULT '',
    as_of TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_assessment_calibration_match
ON assessment_calibration_samples(client, job_type, id DESC);
"""


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _clean(value: Any, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_calibration_schema(conn: sqlite3.Connection) -> None:
    """幂等建表（schema.py 的 ensure_schema 也含同 DDL；此处兜底供独立连接使用）。"""
    conn.executescript(_SCHEMA_DDL)


def normalize_job_type(title: Any) -> str:
    """岗位类型口径：岗位 title 去括号补充说明、压空白，作为"同岗位类型"匹配键。"""
    text = " ".join(str(title or "").split())
    text = re.split(r"[（(【\[]", text)[0].strip()
    return text[:60]


def tag_dimensions(note: Any) -> list[str]:
    """改判 note → 维度标签（确定性关键词打标；都不命中 → ["overall"]，不瞎猜）。"""
    text = str(note or "")
    tags = [name for name in METRIC_DIMENSIONS if any(keyword in text for keyword in _DIMENSION_KEYWORDS[name])]
    return tags or ["overall"]


# ---------------------------------------------------------------------------
# 1. 改判样例库：顾问动作写回时同步（modified/rejected 入库，其余动作移除）
# ---------------------------------------------------------------------------

def sync_calibration_sample(
    conn: sqlite3.Connection,
    *,
    artifact_id: str,
    doc: dict[str, Any],
) -> dict[str, Any]:
    """按 assessment 当前 advisor_action 同步校准样例（幂等 upsert / 移除）。

    - action ∉ {modified, rejected}（采纳/撤回 pending）→ 移除既有样例，返回 stored=False；
    - action ∈ {modified, rejected} → 打维度标签 + 记录机器原判；note 或机器原判命中
      敏感因子（年龄/性别/婚育/户籍）→ 拒绝入库 + 记 blocked 扫描日志（不抛错，
      顾问动作写回本身照常成功，红线只拦样例）。
    不 commit（调用方决定事务）。
    """
    from .candidate_assessment import scan_sensitive  # 延迟导入避免环依赖

    ensure_calibration_schema(conn)
    artifact_id = str(artifact_id)
    action = str(doc.get("advisor_action") or "").strip()
    if action not in SAMPLE_ACTIONS:
        conn.execute(f"DELETE FROM {TABLE} WHERE artifact_id=?", (artifact_id,))
        return {"stored": False, "reason": "action_not_sampled", "advisor_action": action}

    note = _clean(doc.get("advisor_note"), 600)
    dimensions = tag_dimensions(note)
    raw_dimensions = doc.get("dimensions") if isinstance(doc.get("dimensions"), dict) else {}
    verdicts: dict[str, str] = {}
    for name in dimensions:
        if name == "overall":
            verdicts[name] = _clean(doc.get("consultant_summary"), 300)
        else:
            dim = raw_dimensions.get(name) if isinstance(raw_dimensions.get(name), dict) else {}
            verdicts[name] = _clean(dim.get("verdict"), 300)

    # 敏感闸：改判口径或机器原判命中敏感因子 → 拒绝入库 + blocked 扫描日志
    hits = scan_sensitive([note, *[text for text in verdicts.values() if text]])
    if hits:
        conn.execute(f"DELETE FROM {TABLE} WHERE artifact_id=?", (artifact_id,))
        conn.execute(
            """
            INSERT INTO candidate_events
            (job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
            VALUES (?,?,?,?,?,datetime('now','localtime'),?,?,'assessment_calibration',?)
            """,
            (
                int(doc.get("candidate_id") or 0),
                None,
                int(doc.get("job_id") or 0),
                "assessment_calibration_sample_blocked",
                "blocked",
                f"评估校准样例拒入：改判内容命中敏感属性词表 {len(hits)} 处，未入校准集",
                _dumps({"hits": hits[:20], "artifact_id": artifact_id, "advisor_action": action}),
                artifact_id,
            ),
        )
        return {"stored": False, "reason": "sensitive_blocked", "hits": hits}

    as_of = _clean(doc.get("updated_at") or doc.get("as_of")) or _now()
    conn.execute(
        f"""
        INSERT INTO {TABLE}
        (artifact_id,candidate_id,job_id,client,job_type,advisor_action,
         dimensions_json,machine_verdicts_json,advisor_note,as_of)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(artifact_id) DO UPDATE SET
          advisor_action=excluded.advisor_action,
          dimensions_json=excluded.dimensions_json,
          machine_verdicts_json=excluded.machine_verdicts_json,
          advisor_note=excluded.advisor_note,
          as_of=excluded.as_of
        """,
        (
            artifact_id,
            int(doc.get("candidate_id") or 0),
            int(doc.get("job_id") or 0),
            _clean(doc.get("client"), 80),
            normalize_job_type(doc.get("job_title")),
            action,
            _dumps(dimensions),
            _dumps(verdicts),
            note,
            as_of,
        ),
    )
    row = conn.execute(f"SELECT id FROM {TABLE} WHERE artifact_id=?", (artifact_id,)).fetchone()
    return {
        "stored": True,
        "sample_id": int(row["id"]) if row else None,
        "dimensions": dimensions,
        "dimension_labels": [DIMENSION_LABELS[name] for name in dimensions],
    }


# ---------------------------------------------------------------------------
# 2. 校准注入：同客户或同岗位类型最近 ≤5 条改判样例 → few-shot prompt 块
# ---------------------------------------------------------------------------

def retrieve_examples(
    conn: sqlite3.Connection,
    *,
    client: str,
    job_type: str,
    limit: int = INJECT_LIMIT,
) -> list[dict[str, Any]]:
    """检索同客户或同岗位类型的最近改判样例（id 倒序，上限 INJECT_LIMIT=5，契约锚定）。

    client/job_type 为空的维度不参与匹配（防空串互相误命中）；两者皆空 → 不检索。
    """
    ensure_calibration_schema(conn)
    clauses: list[str] = []
    params: list[Any] = []
    if str(client or "").strip():
        clauses.append("client=?")
        params.append(str(client).strip())
    if str(job_type or "").strip():
        clauses.append("job_type=?")
        params.append(str(job_type).strip())
    if not clauses:
        return []
    limit = max(1, min(int(limit or INJECT_LIMIT), INJECT_LIMIT))
    rows = conn.execute(
        f"SELECT * FROM {TABLE} WHERE {' OR '.join(clauses)} ORDER BY id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    examples: list[dict[str, Any]] = []
    for row in rows:
        examples.append(
            {
                "sample_id": int(row["id"]),
                "artifact_id": str(row["artifact_id"]),
                "advisor_action": str(row["advisor_action"]),
                "dimensions": _loads(row["dimensions_json"], []),
                "machine_verdicts": _loads(row["machine_verdicts_json"], {}),
                "advisor_note": str(row["advisor_note"] or ""),
                "client": str(row["client"] or ""),
                "job_type": str(row["job_type"] or ""),
                "as_of": str(row["as_of"] or ""),
            }
        )
    return examples


def build_prompt_block(examples: list[dict[str, Any]]) -> dict[str, Any]:
    """改判样例 → 注入 payload 的 calibration 块（只进 prompt，不落 artifact 正文）。

    调用方约定：examples 为空时不要调用本函数，payload 整个不出现 calibration 键
    （契约：无样例时 prompt 不含校准段）。
    """
    items: list[dict[str, Any]] = []
    for example in examples[:INJECT_LIMIT]:
        dimensions = [str(name) for name in example.get("dimensions") or []]
        verdicts = example.get("machine_verdicts") if isinstance(example.get("machine_verdicts"), dict) else {}
        machine_verdict = "；".join(
            f"{DIMENSION_LABELS.get(name, name)}：{_clean(text, 120)}"
            for name, text in verdicts.items()
            if str(text or "").strip()
        )
        items.append(
            {
                "action": str(example.get("advisor_action") or ""),
                "dimensions": [DIMENSION_LABELS.get(name, name) for name in dimensions],
                "machine_verdict": machine_verdict[:300],
                "advisor_correction": _clean(example.get("advisor_note"), 200) or "（顾问未写改判口径，仅否决整体结论）",
                "client": _clean(example.get("client"), 60),
                "job_type": _clean(example.get("job_type"), 60),
                "as_of": _clean(example.get("as_of"), 20),
            }
        )
    return {"instruction": CALIBRATION_INSTRUCTION, "examples": items}


# ---------------------------------------------------------------------------
# 3. 采纳率度量（顾问点头率）：维度 × 客户聚合，数据不足如实 null
# ---------------------------------------------------------------------------

def compute_metrics(conn: sqlite3.Connection, *, min_n: int = METRICS_MIN_N) -> dict[str, Any]:
    """按维度×客户聚合采纳/改判/否决率。

    口径（写死，验收②锚定）：
    - totals 与库内 candidate_assessment artifact 的 advisor_action 实际分布严格一致；
    - accepted（采纳）是对整份评估的采纳，五维各记一次采纳；
    - modified/rejected 按校准样例的维度标签归组（无样例记录的历史改判归 overall）；
    - pending（未决策）不进任何分组；
    - 分组 total < min_n 时三个率一律 null（数据不足不硬算）。
    """
    ensure_calibration_schema(conn)
    sample_dims: dict[str, list[str]] = {}
    for row in conn.execute(f"SELECT artifact_id,dimensions_json FROM {TABLE}").fetchall():
        dims = [str(name) for name in _loads(row["dimensions_json"], []) if str(name) in DIMENSION_LABELS]
        sample_dims[str(row["artifact_id"])] = dims or ["overall"]

    totals = {"assessments": 0, "pending": 0, "accepted": 0, "modified": 0, "rejected": 0}
    groups: dict[tuple[str, str], dict[str, int]] = {}
    rows = conn.execute(
        "SELECT artifact_id,metadata_json FROM agent_artifacts WHERE artifact_type='candidate_assessment'"
    ).fetchall()
    for row in rows:
        doc = _loads(row["metadata_json"], {})
        if not isinstance(doc, dict):
            continue
        action = str(doc.get("advisor_action") or "pending")
        if action not in ("pending", "accepted", "modified", "rejected"):
            action = "pending"
        client = _clean(doc.get("client"), 80) or "未标注客户"
        totals["assessments"] += 1
        totals[action] += 1
        if action == "accepted":
            for name in METRIC_DIMENSIONS:
                groups.setdefault((client, name), {"accepted": 0, "modified": 0, "rejected": 0})["accepted"] += 1
        elif action in SAMPLE_ACTIONS:
            for name in sample_dims.get(str(row["artifact_id"]), ["overall"]):
                groups.setdefault((client, name), {"accepted": 0, "modified": 0, "rejected": 0})[action] += 1

    group_list: list[dict[str, Any]] = []
    for (client, name), counts in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1])):
        total = counts["accepted"] + counts["modified"] + counts["rejected"]
        sufficient = total >= int(min_n)
        group_list.append(
            {
                "client": client,
                "dimension": name,
                "dimension_label": DIMENSION_LABELS.get(name, name),
                "total": total,
                "accepted": counts["accepted"],
                "modified": counts["modified"],
                "rejected": counts["rejected"],
                "acceptance_rate": round(counts["accepted"] / total, 4) if sufficient else None,
                "modified_rate": round(counts["modified"] / total, 4) if sufficient else None,
                "rejected_rate": round(counts["rejected"] / total, 4) if sufficient else None,
            }
        )
    return {
        "ok": True,
        "generated_at": _now(),
        "min_n": int(min_n),
        "totals": totals,
        "groups": group_list,
        "labels": {
            "title": "评估校准 · 顾问点头率",
            "acceptance_rate": "顾问点头率",
            "modified_rate": "改判率",
            "rejected_rate": "否决率",
            "insufficient": "数据不足",
        },
    }


# ---------------------------------------------------------------------------
# 4. 校准报告（周度手动触发）：markdown → work/calibration/（不进 git）
# ---------------------------------------------------------------------------

def default_calibration_dir() -> Path:
    """报告输出目录：<主仓>/work/calibration/（gitignore 已排除 work/）。"""
    return Path(__file__).resolve().parents[2] / "work" / "calibration"


def generate_report(
    conn: sqlite3.Connection,
    *,
    out_dir: str | Path | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """生成校准周报 markdown：本周改判集中的维度 / 客户口径观察 / 系统性偏差建议 / 样例摘录。

    确定性聚合（不经 LLM）；报告是内部留档文件（可含改判 note 原文），只写
    work/calibration/，不进 git、不进任何对外输出。无样例也如实出报（本周无改判）。
    """
    ensure_calibration_schema(conn)
    today = today or date.today()
    since = today - timedelta(days=REPORT_WINDOW_DAYS - 1)
    rows = conn.execute(
        f"SELECT * FROM {TABLE} WHERE date(created_at) >= ? ORDER BY id DESC LIMIT 200",
        (since.strftime("%Y-%m-%d"),),
    ).fetchall()
    samples: list[dict[str, Any]] = []
    by_action = {"modified": 0, "rejected": 0}
    by_dimension: dict[str, int] = {}
    by_client: dict[str, int] = {}
    for row in rows:
        sample = {
            "advisor_action": str(row["advisor_action"]),
            "dimensions": _loads(row["dimensions_json"], []),
            "machine_verdicts": _loads(row["machine_verdicts_json"], {}),
            "advisor_note": str(row["advisor_note"] or ""),
            "client": str(row["client"] or "") or "未标注客户",
            "job_type": str(row["job_type"] or ""),
            "as_of": str(row["as_of"] or ""),
        }
        samples.append(sample)
        by_action[sample["advisor_action"]] = by_action.get(sample["advisor_action"], 0) + 1
        by_client[sample["client"]] = by_client.get(sample["client"], 0) + 1
        for name in sample["dimensions"]:
            if name in DIMENSION_LABELS:
                by_dimension[name] = by_dimension.get(name, 0) + 1

    total = len(samples)
    top_dimensions = sorted(by_dimension.items(), key=lambda item: (-item[1], item[0]))
    top_clients = sorted(by_client.items(), key=lambda item: (-item[1], item[0]))

    # 系统性偏差建议（确定性规则，可解释）
    suggestions: list[str] = []
    if total >= 2:
        for name, count in top_dimensions:
            if count >= 2 and count / total >= 0.5:
                suggestions.append(
                    f"改判集中在「{DIMENSION_LABELS[name]}」维度（{count}/{total} 条，占比 {round(count / total * 100)}%），"
                    "建议复核该维判断口径与证据阈值。"
                )
                break
        for client, count in top_clients:
            if count >= 2 and count / total >= 0.4:
                suggestions.append(
                    f"客户「{client}」改判占比偏高（{count}/{total} 条），建议与该客户对齐口径，"
                    "并把确认的口味沉淀进知识库客户档案。"
                )
                break
        if by_action.get("rejected", 0) > by_action.get("modified", 0):
            suggestions.append("本周否决多于改判，说明评估整体方向偏差较大，建议复查评估 prompt 与证据闸口径。")
    if not suggestions:
        suggestions.append("本周改判样本较少，暂未发现系统性偏差；继续积累改判样例后再观察。")

    window = f"{since.strftime('%Y-%m-%d')} ~ {today.strftime('%Y-%m-%d')}"
    lines = [
        f"# 评估校准周报（{window}）",
        "",
        f"- 生成时间：{_now()}",
        "- 口径：改判样例 = 顾问改判/否决的判人评估回流（只存口径与维度标签，不存简历原文）；本报告仅供本机内部使用，不得外发。",
        "",
        "## 一、本周改判概览",
        "",
        f"- 改判样例 {total} 条（改判 {by_action.get('modified', 0)} 条 / 否决 {by_action.get('rejected', 0)} 条），涉及客户 {len(by_client)} 家。",
        "",
        "## 二、改判集中的维度",
        "",
    ]
    if top_dimensions:
        for name, count in top_dimensions:
            share = round(count / total * 100) if total else 0
            lines.append(f"- {DIMENSION_LABELS[name]}：{count} 条（{share}%）")
    else:
        lines.append("- 本周无改判样例。")
    lines.extend(["", "## 三、客户口径观察", ""])
    if top_clients:
        for client, count in top_clients:
            lines.append(f"- {client}：{count} 条")
    else:
        lines.append("- 本周无改判样例。")
    lines.extend(["", "## 四、系统性偏差建议", ""])
    lines.extend(f"- {text}" for text in suggestions)
    lines.extend(["", "## 五、改判样例摘录（内部留档，勿外发）", ""])
    if samples:
        lines.append("| 日期 | 客户 | 岗位类型 | 动作 | 维度 | 机器原判 | 顾问口径 |")
        lines.append("|---|---|---|---|---|---|---|")
        action_labels = {"modified": "改判", "rejected": "否决"}
        for sample in samples[:50]:
            verdicts = sample["machine_verdicts"] if isinstance(sample["machine_verdicts"], dict) else {}
            machine = "；".join(_clean(text, 60) for text in verdicts.values() if str(text or "").strip()) or "-"
            dims = "、".join(DIMENSION_LABELS.get(str(name), str(name)) for name in sample["dimensions"])
            note = _clean(sample["advisor_note"], 120).replace("|", "｜") or "（未写口径）"
            lines.append(
                f"| {sample['as_of'][:10]} | {sample['client']} | {sample['job_type'] or '-'} "
                f"| {action_labels.get(sample['advisor_action'], sample['advisor_action'])} | {dims} "
                f"| {machine.replace('|', '｜')} | {note} |"
            )
    else:
        lines.append("- 本周无改判样例。")
    lines.extend(["", "---", "改判样例只影响判断口径，不放宽证据强约束；校准注入内容永不出现在对外文本。", ""])

    directory = Path(out_dir) if out_dir else default_calibration_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"calibration_report_{today.strftime('%Y%m%d')}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "ok": True,
        "path": str(path),
        "window": {"since": since.strftime("%Y-%m-%d"), "until": today.strftime("%Y-%m-%d")},
        "stats": {
            "samples": total,
            "by_action": by_action,
            "by_dimension": {DIMENSION_LABELS.get(name, name): count for name, count in top_dimensions},
            "by_client": dict(top_clients),
            "suggestions": suggestions,
        },
    }
