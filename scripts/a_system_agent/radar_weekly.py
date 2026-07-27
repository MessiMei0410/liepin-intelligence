"""S7-3：雷达周报生成器 + Copilot 周报提醒推送（radar_weekly_report artifact，schema_version=radar_weekly_v1）。

口径来源（事实源）：
- 任务卡 docs/TASKCARD_S7-3_雷达定时化_20260727.md（范围/红线/验收）
- PRD docs/ASA_PRD_S7_人才流动雷达_2026-07-24.md §3（数据模型）/§5（S7-3 行）

红线（写死，违反即返工）：
- 周报内容全部来自库内 radar_scan artifact，不现编；榜单变化对比基于前后两期 artifact，
  缺上周则如实标注"首期，无对比基线"；
- Copilot 推送不含敏感细节：只推条数和入口（X 家新信号、Y 家建议发起 Mapping），
  不推具体人名/公司负面；不弹窗、不打扰，只是浮窗上下文里一条提醒；
- 过期是降权不是删除：周报只统计过期条数，历史信号保留可查；
- 动作只建议不执行：发起 Mapping / 激活存量均由顾问本人执行。
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from . import radar_scan

ARTIFACT_TYPE = "radar_weekly_report"
SCHEMA_VERSION = "radar_weekly_v1"

REQUIRED_KEYS = ("schema_version", "report_date", "top_signals", "action_summary", "copilot_hint")

TOP_SIGNALS_LIMIT = 5
# Copilot 推送通道（复用 publishCopilotContext 的服务端入口 /api/asa/floating/context）
COPILOT_PUSH_TIMEOUT = 3.0


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def default_workbench_url() -> str:
    """工作台服务地址（Copilot 仲裁层所在进程）；环境变量 ASA_WORKBENCH_URL 可覆盖。"""
    return os.environ.get("ASA_WORKBENCH_URL", "http://127.0.0.1:8765").rstrip("/")


# ---------------------------------------------------------------------------
# 1. 读取侧：最近两期 radar_scan artifact（榜单变化对比基线）
# ---------------------------------------------------------------------------

def load_recent_scan_docs(conn: Any, *, limit: int = 2) -> list[dict[str, Any]]:
    """最近 limit 期雷达榜单（新→旧）；表缺失/无数据返回空列表。"""
    try:
        rows = conn.execute(
            """
            SELECT artifact_id,metadata_json,created_at FROM agent_artifacts
            WHERE artifact_type=? ORDER BY id DESC LIMIT ?
            """,
            (radar_scan.ARTIFACT_TYPE, max(1, int(limit))),
        ).fetchall()
    except Exception:  # noqa: BLE001 表不存在按无榜单处理
        return []
    docs: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row) if hasattr(row, "keys") else {
            "artifact_id": row[0], "metadata_json": row[1], "created_at": row[2],
        }
        doc = radar_scan._loads(record.get("metadata_json"), {})
        if isinstance(doc, dict) and doc.get("schema_version") == radar_scan.SCHEMA_VERSION:
            docs.append(
                {
                    "artifact_id": str(record.get("artifact_id") or ""),
                    "created_at": str(record.get("created_at") or ""),
                    "doc": doc,
                }
            )
    return docs


# ---------------------------------------------------------------------------
# 2. 周报组装（Top 信号 / 过期统计 / 榜单变化对比 / 建议动作汇总 / Copilot 提醒）
# ---------------------------------------------------------------------------

def _signal_weight(signal: dict[str, Any]) -> float:
    return radar_scan._TYPE_WEIGHT.get(str(signal.get("type")), 1.0) * radar_scan._CONFIDENCE_FACTOR.get(
        str(signal.get("confidence")), 0.4
    )


def _ranking_index(ranking: list[dict[str, Any]]) -> dict[str, int]:
    """{规范化公司名: 榜次（1 起）}。"""
    from . import knowledge_base

    index: dict[str, int] = {}
    for position, entry in enumerate(ranking or [], start=1):
        norm = knowledge_base.normalize_client_name(str(entry.get("company") or ""))
        if norm:
            index[norm] = position
    return index


def build_ranking_changes(
    latest_ranking: list[dict[str, Any]],
    previous_ranking: list[dict[str, Any]],
) -> dict[str, Any]:
    """榜单变化对比：新进榜 / 掉出 / 上升 / 下降（公司名 + 榜次，业务语言）。"""
    from . import knowledge_base

    latest_index = _ranking_index(latest_ranking)
    previous_index = _ranking_index(previous_ranking)
    # 公司展示名：以最新一期为准，掉出的取上一期名称
    names: dict[str, str] = {}
    for ranking in (previous_ranking or [], latest_ranking or []):
        for entry in ranking:
            norm = knowledge_base.normalize_client_name(str(entry.get("company") or ""))
            if norm:
                names[norm] = str(entry.get("company") or "")

    new_entries = sorted(
        ({"company": names[norm], "rank": rank} for norm, rank in latest_index.items() if norm not in previous_index),
        key=lambda item: item["rank"],
    )
    dropped = sorted(
        ({"company": names[norm], "previous_rank": rank} for norm, rank in previous_index.items() if norm not in latest_index),
        key=lambda item: item["previous_rank"],
    )
    risen: list[dict[str, Any]] = []
    fallen: list[dict[str, Any]] = []
    for norm, rank in latest_index.items():
        previous_rank = previous_index.get(norm)
        if previous_rank is None or previous_rank == rank:
            continue
        record = {"company": names[norm], "from": previous_rank, "to": rank}
        (risen if rank < previous_rank else fallen).append(record)
    risen.sort(key=lambda item: item["to"])
    fallen.sort(key=lambda item: item["to"])
    return {"new_entries": new_entries, "dropped": dropped, "risen": risen, "fallen": fallen}


def build_weekly_report(
    conn: Any,
    *,
    today: Any = None,
    top_n: int = TOP_SIGNALS_LIMIT,
) -> dict[str, Any]:
    """组装 radar_weekly_v1 周报文档：内容全部来自库内最近两期 radar_scan artifact，不现编。

    无榜单抛 LookupError（路由层映射 404）；缺上一期 → baseline.has_baseline=False，
    如实标注"首期，无对比基线"。
    """
    today = today if isinstance(today, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", str(today or "")) else _today()
    scans = load_recent_scan_docs(conn, limit=2)
    if not scans:
        raise LookupError("还没有雷达榜单：请先发起一次扫描（POST /api/v1/radar/scans）")
    latest = scans[0]
    previous = scans[1] if len(scans) > 1 else None
    latest_doc = latest["doc"]
    scan_stats = latest_doc.get("stats") or {}

    signals = [signal for signal in latest_doc.get("signals") or [] if isinstance(signal, dict)]
    active = [
        signal
        for signal in signals
        if not radar_scan.is_signal_expired(str(signal.get("as_of") or ""), today)
    ]
    expired_count = len(signals) - len(active)

    top_signals = sorted(active, key=lambda signal: (-_signal_weight(signal), str(signal.get("company") or "")))[
        : max(1, int(top_n))
    ]
    top_briefs = [
        {
            "company": str(signal.get("company") or ""),
            "type": str(signal.get("type") or ""),
            "type_label": radar_scan.SIGNAL_TYPE_LABELS.get(str(signal.get("type")), str(signal.get("type"))),
            "summary": str(signal.get("summary") or ""),
            "implication": str(signal.get("implication") or ""),
            "as_of": str(signal.get("as_of") or ""),
            "source_urls": [str(url) for url in signal.get("source_urls") or []],
            "linked_action": str(signal.get("linked_action") or "watch"),
        }
        for signal in top_signals
    ]

    action_summary = {"mapping": 0, "activate": 0, "watch": 0}
    action_companies: dict[str, list[str]] = {"mapping": [], "activate": [], "watch": []}
    for entry in latest_doc.get("ranking") or []:
        action = str(entry.get("suggested_action") or "watch")
        if action not in action_summary:
            action = "watch"
        action_summary[action] += 1
        action_companies[action].append(str(entry.get("company") or ""))

    if previous is not None and str((previous["doc"] or {}).get("scan_date") or "") != str(latest_doc.get("scan_date") or ""):
        baseline = {
            "has_baseline": True,
            "scan_artifact_id": previous["artifact_id"],
            "scan_date": str(previous["doc"].get("scan_date") or ""),
            "note": "",
        }
        ranking_changes = build_ranking_changes(
            latest_doc.get("ranking") or [], previous["doc"].get("ranking") or []
        )
    else:
        baseline = {"has_baseline": False, "scan_artifact_id": "", "scan_date": "", "note": "首期，无对比基线"}
        ranking_changes = None

    signal_company_count = len({str(signal.get("company") or "") for signal in active})
    # Copilot 提醒：只推条数和入口，不含公司/人名等敏感细节（任务卡红线）
    copilot_hint = {
        "text": (
            f"本周雷达周报出了：{signal_company_count} 家新信号，"
            f"{action_summary['mapping']} 家建议发起 Mapping"
        ),
        "entry": "radar",
        "entry_hint": "顾问点开即看榜单页",
        "report_date": today,
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "report_date": today,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scan_artifact_id": latest["artifact_id"],
        "scan_date": str(latest_doc.get("scan_date") or ""),
        "scan_stats": {
            "companies_scanned": int(scan_stats.get("companies_scanned") or 0),
            "signals_found": int(scan_stats.get("signals_found") or 0),
            "companies_with_signals": int(scan_stats.get("companies_with_signals") or 0),
        },
        "top_signals": top_briefs,
        "active_signal_count": len(active),
        "expired_signal_count": expired_count,
        "baseline": baseline,
        "ranking_changes": ranking_changes,
        "action_summary": action_summary,
        "action_companies": action_companies,
        "copilot_hint": copilot_hint,
        "red_lines": [
            "周报内容全部来自库内榜单 artifact，不现编；缺上周基线如实标注",
            "过期是降权不是删除：历史信号保留可查",
            "Copilot 提醒只推条数和入口，不含人名/公司名等敏感细节",
            "雷达只出建议，所有对外动作由顾问本人执行",
        ],
    }


# ---------------------------------------------------------------------------
# 3. 周报 markdown（全业务语言）+ 持久化（同日幂等 upsert）
# ---------------------------------------------------------------------------

def render_weekly_markdown(doc: dict[str, Any]) -> str:
    """周报 markdown：本周 Top 信号 / 过期统计 / 榜单变化对比 / 建议动作汇总（全业务语言）。"""
    stats = doc.get("scan_stats") or {}
    lines = [
        f"# 人才流动雷达 · 周报（{doc.get('report_date')}）",
        "",
        f"- 数据来源：{doc.get('scan_date')} 榜单（扫描公司 {stats.get('companies_scanned', 0)} 家，"
        f"信号 {stats.get('signals_found', 0)} 条，全部带来源链接）",
        "- 信号全部来自公开信息；『可能意味着』是推测，仅供顾问本人判断，系统不自动触达任何人选",
        "",
        "## 本周 Top 信号",
    ]
    top_signals = doc.get("top_signals") or []
    if not top_signals:
        lines.extend(["", "本周未见达到上榜强度的公开信号。"])
    for index, signal in enumerate(top_signals, start=1):
        line = (
            f"{index}. 【{signal.get('type_label')}】{signal.get('company')}：{signal.get('summary')}"
            f"（{signal.get('as_of')}）"
        )
        implication = str(signal.get("implication") or "").strip()
        if implication:
            line += f"｜可能意味着：{implication}"
        if signal.get("source_urls"):
            line += f"｜来源：{'；'.join(signal['source_urls'])}"
        lines.extend(["", line])

    expired_count = int(doc.get("expired_signal_count") or 0)
    lines.extend(["", "## 信号过期", ""])
    if expired_count:
        lines.append(
            f"本周有 {expired_count} 条信号超过 60 天有效期，已自动降权、不再作为上榜理由"
            f"（历史信号保留可查，未删除）。"
        )
    else:
        lines.append("本周没有信号过期。")

    baseline = doc.get("baseline") or {}
    lines.extend(["", "## 榜单变化对比", ""])
    if not baseline.get("has_baseline"):
        lines.append("首期，无对比基线——下周起这里会给出新进榜/掉出/升降对比。")
    else:
        lines.append(f"对比基线：{baseline.get('scan_date')} 榜单。")
        changes = doc.get("ranking_changes") or {}
        for title, key, formatter in (
            ("新进榜", "new_entries", lambda item: f"{item['company']}（第 {item['rank']} 名）"),
            ("掉出榜单", "dropped", lambda item: f"{item['company']}（上期第 {item['previous_rank']} 名）"),
            ("排名上升", "risen", lambda item: f"{item['company']}（第 {item['from']} 名 → 第 {item['to']} 名）"),
            ("排名下降", "fallen", lambda item: f"{item['company']}（第 {item['from']} 名 → 第 {item['to']} 名）"),
        ):
            items = changes.get(key) or []
            brief = "、".join(formatter(item) for item in items) if items else "无"
            lines.append(f"- {title}：{brief}")

    action_summary = doc.get("action_summary") or {}
    action_companies = doc.get("action_companies") or {}
    lines.extend(["", "## 建议动作汇总（由顾问本人执行）", ""])
    for action, label in (("mapping", "发起 Mapping 直挖"), ("activate", "激活存量人选"), ("watch", "观望")):
        companies = action_companies.get(action) or []
        brief = f"：{'、'.join(companies)}" if companies else ""
        lines.append(f"- {label} {int(action_summary.get(action) or 0)} 家{brief}")
    return "\n".join(lines) + "\n"


def write_weekly_markdown(doc: dict[str, Any], *, radar_dir: str | Path | None = None) -> str:
    """周报落盘 work/radar/（不进 git）；返回文件路径字符串。"""
    directory = Path(radar_dir) if radar_dir else radar_scan.default_radar_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"radar_weekly_{doc.get('report_date')}.md"
    path.write_text(render_weekly_markdown(doc), encoding="utf-8")
    return str(path)


def validate_weekly_report(doc: Any) -> list[str]:
    """校验 radar_weekly_v1 文档；返回错误列表（空=通过）。任何错误都拒绝写入。"""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["radar_weekly_report 必须是对象"]
    if doc.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version 必须是 {SCHEMA_VERSION}（实际：{doc.get('schema_version')}）")
    for key in REQUIRED_KEYS:
        if key not in doc:
            errors.append(f"缺必备键 {key}")
    if errors:
        return errors
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(doc.get("report_date") or "")):
        errors.append("report_date 必须是 YYYY-MM-DD")
    if not isinstance(doc.get("top_signals"), list):
        errors.append("top_signals 必须是数组")
    action_summary = doc.get("action_summary")
    if not isinstance(action_summary, dict) or not all(
        isinstance(action_summary.get(key), int) for key in ("mapping", "activate", "watch")
    ):
        errors.append("action_summary 必须含 mapping/activate/watch 整数计数")
    hint = doc.get("copilot_hint")
    if not isinstance(hint, dict) or not str(hint.get("text") or "").strip():
        errors.append("copilot_hint.text 必须非空（Copilot 提醒文案，只含条数和入口）")
    return errors


def upsert_weekly_report(conn: Any, doc: dict[str, Any], *, radar_dir: str | Path | None = None) -> str:
    """校验 + 同日幂等 upsert：artifact_id=radar_weekly_<report_date>，同日重复生成更新同一 artifact
    （version 自增 + history，上限 10 条）。校验不过抛 ValueError，整条拒写。
    """
    errors = validate_weekly_report(doc)
    if errors:
        raise ValueError("radar_weekly_report 校验失败，拒绝写入：" + "；".join(errors))
    artifact_id = f"radar_weekly_{doc['report_date']}"
    existing = conn.execute(
        """
        SELECT artifact_id,metadata_json FROM agent_artifacts
        WHERE artifact_id=? AND artifact_type=? LIMIT 1
        """,
        (artifact_id, ARTIFACT_TYPE),
    ).fetchone()
    file_path = write_weekly_markdown(doc, radar_dir=radar_dir)
    doc["report_file"] = file_path
    title = f"人才流动雷达周报 {doc['report_date']}"
    content = render_weekly_markdown(doc)
    if existing:
        previous = radar_scan._loads(existing["metadata_json"], {})
        history = list(previous.get("history") or [])
        history.append(
            {
                "version": int(previous.get("version") or 1),
                "generated_at": previous.get("generated_at"),
            }
        )
        doc["version"] = int(previous.get("version") or 1) + 1
        doc["history"] = history[-10:]
        conn.execute(
            """
            UPDATE agent_artifacts SET content=?,metadata_json=?,validation_status='passed',title=?,file_path=?
            WHERE artifact_id=?
            """,
            (content, _dumps(doc), f"{title} v{doc['version']}", file_path, artifact_id),
        )
        return artifact_id
    doc["version"] = 1
    doc["history"] = []
    conn.execute(
        """
        INSERT INTO agent_artifacts
        (artifact_id,goal_id,workflow_id,step_id,artifact_type,title,mime_type,file_path,content,metadata_json,validation_status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            artifact_id,
            "radar",
            "radar_global",
            None,
            ARTIFACT_TYPE,
            f"{title} v1",
            "text/markdown",
            file_path,
            content,
            _dumps(doc),
            "passed",
        ),
    )
    return artifact_id


def get_latest_weekly_report(conn: Any) -> dict[str, Any] | None:
    """读取最新一期雷达周报；不存在返回 None。"""
    try:
        row = conn.execute(
            """
            SELECT artifact_id,title,file_path,content,metadata_json,created_at
            FROM agent_artifacts WHERE artifact_type=? ORDER BY id DESC LIMIT 1
            """,
            (ARTIFACT_TYPE,),
        ).fetchone()
    except Exception:  # noqa: BLE001 表不存在按无周报处理
        return None
    if row is None:
        return None
    record = dict(row) if hasattr(row, "keys") else {
        "artifact_id": row[0], "title": row[1], "file_path": row[2],
        "content": row[3], "metadata_json": row[4], "created_at": row[5],
    }
    doc = radar_scan._loads(record.get("metadata_json"), {})
    return {
        "artifact_id": str(record.get("artifact_id") or ""),
        "title": str(record.get("title") or ""),
        "file_path": str(record.get("file_path") or ""),
        "content": str(record.get("content") or ""),
        "weekly_report": doc,
        "created_at": str(record.get("created_at") or ""),
    }


# ---------------------------------------------------------------------------
# 4. Copilot 周报提醒推送（复用 publishCopilotContext 服务端通道 /api/asa/floating/context）
# ---------------------------------------------------------------------------

def push_copilot_hint(
    hint: dict[str, Any],
    *,
    base_url: str = "",
    timeout: float = COPILOT_PUSH_TIMEOUT,
) -> dict[str, Any]:
    """把周报提醒写进 Copilot 仲裁层（服务端调 /api/asa/floating/context，与前端 publishCopilotContext 同通道）。

    红线：只推条数和入口（hint.text 在组装侧已锚死为"X 家新信号，Y 家建议发起 Mapping"），
    不含人名/公司名；不弹窗（explicit=False），只是浮窗上下文里一条提醒。
    推送失败绝不阻断周报：返回 {"pushed": False, "note": 原因} 留痕。
    """
    text = str((hint or {}).get("text") or "").strip()
    if not text:
        return {"pushed": False, "note": "提醒文案为空，未推送"}
    url = f"{base_url or default_workbench_url()}/api/asa/floating/context"
    payload = {
        "surface": "a_system",
        "instance_id": "asa-agent",
        "trigger": "radar_weekly_report",
        "explicit": False,
        "user_selected": False,
        "page_focused": False,
        "page_visible": False,
        "context": {
            "page": str((hint or {}).get("entry") or "radar"),
            "type": "radar_weekly_report",
            "label": "人才流动雷达",
            "subtitle": "ASA Agent",
            "notice": text,
            "entry_hint": str((hint or {}).get("entry_hint") or ""),
            "report_date": str((hint or {}).get("report_date") or ""),
        },
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(0.5, float(timeout))) as response:
            if 200 <= int(response.status) < 300:
                return {"pushed": True, "note": ""}
            return {"pushed": False, "note": f"工作台服务返回 HTTP {response.status}（周报已正常生成）"}
    except Exception as exc:  # noqa: BLE001 推送失败不阻断周报，原因留痕
        return {"pushed": False, "note": f"Copilot 提醒未送达（周报已正常生成）：{exc}"[:200]}
