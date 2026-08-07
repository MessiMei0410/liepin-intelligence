from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, time as datetime_time, timedelta, timezone as datetime_timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


CATALOG_VERSION = "2026-08-03"
RESULT_VERSION = "analysis_result_v1"
MAX_RESULT_ROWS = 10
RUN_TTL_DAYS = 7
DEFAULT_TIMEZONE = "Asia/Shanghai"
SCHEDULE_KINDS = {"manual", "daily", "weekly"}
TEMPLATE_RUN_TRIGGERS = {"manual", "schedule"}

# 工作台五分组：按「需要谁行动」归类，而不是按数量堆叠。
# decision 待判断（顾问判断/动作）、waiting_client 待客户、risk 风险/逾期、
# running 运行中、delivered 最近交付。flow inbox 的 queue 在这里映射成 lane。
WORKBENCH_LANES = ("decision", "running", "waiting_client", "risk", "delivered")
FLOW_DECISION_QUEUES = {"待复核", "待核验", "待联系", "已回复"}
FLOW_RISK_QUEUES = {"超时", "异常"}
# 待客户信号：已推荐待反馈 / 已发客户报告待确认，由阶段与最近事件文本推导；没有命中即为空，不造假。
CLIENT_WAIT_TOKENS = ("已推荐", "待客户", "客户反馈", "客户确认", "待反馈", "报告已发")

ACTIVE_JOB_PREDICATE = """COALESCE(j.lifecycle_stage,'') IN
    ('sourcing','published','active_pipeline','client_feedback','offer')
    AND COALESCE(j.status,'') NOT IN ('误归属-已迁移到视源电子','只读快照')"""

CATALOGS: dict[str, dict[str, Any]] = {
    "operations_overview": {"label": "经营概览", "fields": {"days"}},
    "job_health": {"label": "岗位健康", "fields": {"job_id", "days"}},
    "talent_search": {"label": "人才查询", "fields": {"job_id", "company", "title", "city", "stage", "limit"}},
    "candidate_compare": {"label": "人选对比", "fields": {"candidate_ids"}},
    "channel_performance": {"label": "渠道效果", "fields": {"days", "job_id"}},
    "workflow_funnel": {"label": "工作流漏斗", "fields": {"workflow_id"}},
    "data_quality": {"label": "数据质量", "fields": set()},
    "delivery_scorecard": {"label": "交付记分卡", "fields": {"job_id", "days"}},
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _flow_item_lane(source: dict[str, Any]) -> str:
    """flow inbox 人选对 → 工作台 lane；普通「进行中」且无客户等待信号的不进工作台（返回空串）。"""
    queue = str(source.get("queue") or "")
    if queue in FLOW_RISK_QUEUES:
        return "risk"
    if queue in FLOW_DECISION_QUEUES:
        return "decision"
    text = " ".join(
        str(source.get(key) or "") for key in ("clean_stage", "last_event_type", "last_event_summary")
    )
    if any(token in text for token in CLIENT_WAIT_TOKENS):
        return "waiting_client"
    return ""


def _metric(metric_id: str, label: str, value: int | float | None, unit: str = "count") -> dict[str, Any]:
    return {
        "id": metric_id,
        "label": label,
        "value": value,
        "unit": unit,
        "definition_id": f"asa.{metric_id}",
        "definition_version": CATALOG_VERSION,
    }


def _scorecard_metric(
    metric_id: str,
    label: str,
    value: int | float | None,
    unit: str,
    sample_size: int,
    note: str,
) -> dict[str, Any]:
    """交付记分卡指标：在通用指标上补样本量与中文口径说明（additive，不改 _metric 口径）。"""
    return {**_metric(metric_id, label, value, unit), "sample_size": sample_size, "note": note}


def _utc_now() -> datetime:
    return datetime.now(datetime_timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(datetime_timezone.utc).isoformat(timespec="seconds")


def _next_schedule_at(
    schedule_kind: str,
    schedule_time: str,
    schedule_weekday: int,
    timezone_name: str,
    *,
    after: datetime | None = None,
) -> str | None:
    if schedule_kind == "manual":
        return None
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"未知时区：{timezone_name}") from exc
    try:
        hour, minute = (int(part) for part in schedule_time.split(":", 1))
        run_time = datetime_time(hour=hour, minute=minute)
    except (TypeError, ValueError) as exc:
        raise ValueError("执行时间必须为 HH:MM") from exc
    current = (after or _utc_now()).astimezone(zone)
    candidate = datetime.combine(current.date(), run_time, tzinfo=zone)
    if schedule_kind == "daily":
        if candidate <= current:
            candidate += timedelta(days=1)
    else:
        candidate += timedelta(days=(schedule_weekday - current.weekday()) % 7)
        if candidate <= current:
            candidate += timedelta(days=7)
    return _utc_text(candidate)


class AnalyticsService:
    """Registered, deterministic analytics over the v3 source of truth."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self._handlers: dict[str, Callable[[sqlite3.Connection, dict[str, Any]], dict[str, Any]]] = {
            "operations_overview": self._operations_overview,
            "job_health": self._job_health,
            "talent_search": self._talent_search,
            "candidate_compare": self._candidate_compare,
            "channel_performance": self._channel_performance,
            "workflow_funnel": self._workflow_funnel,
            "data_quality": self._data_quality,
            "delivery_scorecard": self._delivery_scorecard,
        }

    def _read_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=3)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=3000")
        deadline = time.monotonic() + 3.0
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        return conn

    def _write_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def catalog(self) -> dict[str, Any]:
        return {
            "ok": True,
            "version": CATALOG_VERSION,
            "items": [
                {"catalog_id": key, "label": value["label"], "allowed_scope_fields": sorted(value["fields"])}
                for key, value in CATALOGS.items()
            ],
        }

    def _normalize_scope(self, catalog_id: str, scope: dict[str, Any] | None) -> dict[str, Any]:
        if catalog_id not in CATALOGS:
            raise ValueError(f"未知分析类型：{catalog_id}")
        raw = dict(scope or {})
        unknown = set(raw) - set(CATALOGS[catalog_id]["fields"])
        if unknown:
            raise ValueError(f"分析范围包含未授权字段：{', '.join(sorted(unknown))}")
        normalized: dict[str, Any] = {}
        for key, value in raw.items():
            if value in (None, "", []):
                continue
            if key in {"job_id", "days", "limit"}:
                normalized[key] = int(value)
            elif key == "candidate_ids":
                normalized[key] = [int(item) for item in list(value)[:MAX_RESULT_ROWS]]
                if not normalized[key]:
                    raise ValueError("candidate_ids 不能为空")
            else:
                normalized[key] = str(value).strip()[:120]
        if "days" in normalized:
            normalized["days"] = max(1, min(normalized["days"], 90))
        if "limit" in normalized:
            normalized["limit"] = max(1, min(normalized["limit"], MAX_RESULT_ROWS))
        return normalized

    def create_run(
        self,
        catalog_id: str,
        question: str = "",
        scope: dict[str, Any] | None = None,
        *,
        supersedes_run_id: str | None = None,
    ) -> dict[str, Any]:
        normalized = self._normalize_scope(catalog_id, scope)
        run_id = f"analysis_{uuid.uuid4().hex}"
        started = time.monotonic()
        status, error = "completed", ""
        try:
            conn = self._read_connection()
            try:
                payload = self._handlers[catalog_id](conn, normalized)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            status, error = "failed", str(exc)[:500]
            payload = {"headline": "分析未完成", "metrics": [], "sections": [], "references": [], "caveats": [error]}
        result = {
            "schema_version": RESULT_VERSION,
            "run_id": run_id,
            "catalog_id": catalog_id,
            "catalog_version": CATALOG_VERSION,
            "status": status,
            "question": str(question or CATALOGS[catalog_id]["label"])[:500],
            "scope": normalized,
            "data_as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
            "headline": payload.get("headline", ""),
            "metrics": payload.get("metrics", []),
            "sections": payload.get("sections", []),
            "references": payload.get("references", []),
            "caveats": payload.get("caveats", []),
            "truncated": bool(payload.get("truncated", False)),
            "suggested_actions": payload.get("suggested_actions", []),
            "supersedes_run_id": supersedes_run_id,
        }
        duration_ms = int((time.monotonic() - started) * 1000)
        conn = self._write_connection()
        try:
            conn.execute(
                """INSERT INTO agent_analysis_runs
                   (run_id,catalog_id,catalog_version,question,scope_json,status,result_json,
                    supersedes_run_id,duration_ms,error,expires_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now','localtime',?))""",
                (run_id, catalog_id, CATALOG_VERSION, result["question"], _json(normalized), status,
                 _json(result), supersedes_run_id, duration_ms, error, f"+{RUN_TTL_DAYS} days"),
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": status != "failed", "result": result, "duration_ms": duration_ms}

    def get_run(self, run_id: str) -> dict[str, Any]:
        conn = self._write_connection()
        try:
            row = conn.execute(
                """SELECT ar.result_json,ar.expires_at,ar.duration_ms,ar.export_path,tr.template_id
                   FROM agent_analysis_runs ar
                   LEFT JOIN agent_analysis_template_runs tr ON tr.analysis_run_id=ar.run_id
                   WHERE ar.run_id=? ORDER BY tr.id DESC LIMIT 1""",
                (run_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            raise LookupError(f"找不到分析记录：{run_id}")
        result = json.loads(str(row["result_json"] or "{}"))
        expired = bool(row["expires_at"] and str(row["expires_at"]) < datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        if expired:
            result["status"] = "expired"
        return {
            "ok": not expired, "result": result, "duration_ms": int(row["duration_ms"] or 0),
            "exported": bool(row["export_path"]),
            "download_url": f"/api/v1/analytics/runs/{run_id}/download" if row["export_path"] else None,
            "template_id": row["template_id"],
        }

    def refresh_run(self, run_id: str) -> dict[str, Any]:
        previous = self.get_run(run_id)["result"]
        return self.create_run(
            str(previous["catalog_id"]), str(previous.get("question") or ""),
            dict(previous.get("scope") or {}), supersedes_run_id=run_id,
        )

    def _normalize_schedule(
        self,
        schedule_kind: str = "manual",
        schedule_enabled: bool = False,
        schedule_time: str = "09:00",
        schedule_weekday: int = 0,
        timezone_name: str = DEFAULT_TIMEZONE,
        *,
        after: datetime | None = None,
    ) -> dict[str, Any]:
        kind = str(schedule_kind or "manual").strip().lower()
        if kind not in SCHEDULE_KINDS:
            raise ValueError("执行频率仅支持 manual、daily、weekly")
        clean_time = str(schedule_time or "09:00").strip()
        weekday = int(schedule_weekday)
        if not 0 <= weekday <= 6:
            raise ValueError("每周执行日必须在 0 到 6 之间")
        clean_timezone = str(timezone_name or DEFAULT_TIMEZONE).strip()
        try:
            ZoneInfo(clean_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"未知时区：{clean_timezone}") from exc
        scheduled = bool(schedule_enabled) and kind != "manual"
        computed_next = _next_schedule_at(
            kind if kind != "manual" else "daily", clean_time, weekday, clean_timezone, after=after,
        )
        next_run_at = computed_next if scheduled else None
        return {
            "schedule_kind": kind,
            "schedule_enabled": scheduled,
            "schedule_time": clean_time,
            "schedule_weekday": weekday,
            "timezone": clean_timezone,
            "next_run_at": next_run_at,
        }

    def _template_item(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "template_id": row["template_id"], "name": row["name"], "catalog_id": row["catalog_id"],
            "question": row["question"], "scope": json.loads(str(row["scope_json"] or "{}")),
            "enabled": bool(row["enabled"]), "schedule_kind": row["schedule_kind"],
            "schedule_enabled": bool(row["schedule_enabled"]), "schedule_time": row["schedule_time"],
            "schedule_weekday": int(row["schedule_weekday"] or 0), "timezone": row["timezone"],
            "next_run_at": row["next_run_at"], "last_run_at": row["last_run_at"],
            "last_status": row["last_status"], "last_run_id": row["last_run_id"],
            "last_result": json.loads(str(row["last_result_json"] or "{}")) or None,
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def list_templates(self) -> dict[str, Any]:
        conn = self._write_connection()
        try:
            rows = conn.execute(
                """SELECT t.*,r.result_json AS last_result_json FROM agent_analysis_templates t
                   LEFT JOIN agent_analysis_runs r ON r.run_id=t.last_run_id
                   WHERE t.enabled=1 ORDER BY t.updated_at DESC,t.id DESC"""
            ).fetchall()
        finally:
            conn.close()
        return {"ok": True, "items": [self._template_item(row) for row in rows]}

    def create_template(
        self,
        name: str,
        catalog_id: str,
        question: str,
        scope: dict[str, Any] | None,
        *,
        schedule_kind: str = "manual",
        schedule_enabled: bool = False,
        schedule_time: str = "09:00",
        schedule_weekday: int = 0,
        timezone_name: str = DEFAULT_TIMEZONE,
    ) -> dict[str, Any]:
        normalized = self._normalize_scope(catalog_id, scope)
        schedule = self._normalize_schedule(
            schedule_kind, schedule_enabled, schedule_time, schedule_weekday, timezone_name,
        )
        clean_name = " ".join(str(name or "").split())[:80]
        if not clean_name:
            raise ValueError("模板名称不能为空")
        template_id = f"template_{uuid.uuid4().hex}"
        conn = self._write_connection()
        try:
            conn.execute(
                """INSERT INTO agent_analysis_templates
                   (template_id,name,catalog_id,question,scope_json,schedule_kind,schedule_enabled,
                    schedule_time,schedule_weekday,timezone,next_run_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    template_id, clean_name, catalog_id, str(question or "")[:500], _json(normalized),
                    schedule["schedule_kind"], int(schedule["schedule_enabled"]), schedule["schedule_time"],
                    schedule["schedule_weekday"], schedule["timezone"], schedule["next_run_at"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "template_id": template_id}

    def update_template(self, template_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "name", "catalog_id", "question", "scope", "schedule_kind", "schedule_enabled",
            "schedule_time", "schedule_weekday", "timezone",
        }
        unknown = set(patch) - allowed
        if unknown:
            raise ValueError(f"固定分析包含未知字段：{', '.join(sorted(unknown))}")
        if not patch:
            raise ValueError("固定分析没有可更新的字段")
        if any(value is None for value in patch.values()):
            raise ValueError("固定分析字段不能设为空值")
        conn = self._write_connection()
        try:
            row = conn.execute(
                "SELECT * FROM agent_analysis_templates WHERE template_id=? AND enabled=1", (template_id,),
            ).fetchone()
            if not row:
                raise LookupError(f"找不到固定分析：{template_id}")
            catalog_id = str(patch.get("catalog_id", row["catalog_id"]))
            scope = self._normalize_scope(
                catalog_id, patch.get("scope", json.loads(str(row["scope_json"] or "{}"))),
            )
            name = " ".join(str(patch.get("name", row["name"]) or "").split())[:80]
            if not name:
                raise ValueError("模板名称不能为空")
            schedule = self._normalize_schedule(
                str(patch.get("schedule_kind", row["schedule_kind"])),
                bool(patch.get("schedule_enabled", row["schedule_enabled"])),
                str(patch.get("schedule_time", row["schedule_time"])),
                int(patch.get("schedule_weekday", row["schedule_weekday"])),
                str(patch.get("timezone", row["timezone"])),
            )
            schedule_fields = {"schedule_kind", "schedule_enabled", "schedule_time", "schedule_weekday", "timezone"}
            if not (set(patch) & schedule_fields):
                schedule["next_run_at"] = row["next_run_at"]
            conn.execute(
                """UPDATE agent_analysis_templates
                   SET name=?,catalog_id=?,question=?,scope_json=?,schedule_kind=?,schedule_enabled=?,
                       schedule_time=?,schedule_weekday=?,timezone=?,next_run_at=?,updated_at=datetime('now','localtime')
                   WHERE template_id=?""",
                (
                    name, catalog_id, str(patch.get("question", row["question"]) or "")[:500], _json(scope),
                    schedule["schedule_kind"], int(schedule["schedule_enabled"]), schedule["schedule_time"],
                    schedule["schedule_weekday"], schedule["timezone"], schedule["next_run_at"], template_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "template_id": template_id}

    def recover_stale_template_runs(self, max_age_minutes: int = 15) -> int:
        cutoff = _utc_text(_utc_now() - timedelta(minutes=max(1, max_age_minutes)))
        completed = _utc_text(_utc_now())
        conn = self._write_connection()
        try:
            rows = conn.execute(
                "SELECT template_run_id,template_id FROM agent_analysis_template_runs WHERE status='running' AND started_at<?",
                (cutoff,),
            ).fetchall()
            if rows:
                ids = [str(row["template_run_id"]) for row in rows]
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE agent_analysis_template_runs SET status='failed',completed_at=?,error=? WHERE template_run_id IN ({placeholders})",
                    (completed, "Core 重启或执行超时，任务已中止", *ids),
                )
                for template_id in {str(row["template_id"]) for row in rows}:
                    conn.execute(
                        "UPDATE agent_analysis_templates SET last_status='failed',last_run_at=? WHERE template_id=? AND last_status='running'",
                        (completed, template_id),
                    )
                conn.commit()
            return len(rows)
        finally:
            conn.close()

    def _claim_template_run(self, template_id: str, trigger: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        if trigger not in TEMPLATE_RUN_TRIGGERS:
            raise ValueError(f"未知固定分析触发方式：{trigger}")
        now = _utc_now()
        now_text = _utc_text(now)
        template_run_id = f"template_run_{uuid.uuid4().hex}"
        conn = self._write_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM agent_analysis_templates WHERE template_id=? AND enabled=1", (template_id,),
            ).fetchone()
            if not row:
                raise LookupError(f"找不到固定分析：{template_id}")
            if trigger == "schedule":
                if not row["schedule_enabled"] or str(row["schedule_kind"]) == "manual" or not row["next_run_at"]:
                    conn.rollback()
                    return None
                if str(row["next_run_at"]) > now_text:
                    conn.rollback()
                    return None
            running = conn.execute(
                "SELECT template_run_id FROM agent_analysis_template_runs WHERE template_id=? AND status='running' LIMIT 1",
                (template_id,),
            ).fetchone()
            next_run_at = row["next_run_at"]
            if trigger == "schedule":
                next_run_at = _next_schedule_at(
                    str(row["schedule_kind"]), str(row["schedule_time"]), int(row["schedule_weekday"] or 0),
                    str(row["timezone"]), after=now,
                )
            status = "skipped" if running else "running"
            error = "已有同模板分析正在运行，本次已跳过" if running else None
            conn.execute(
                """INSERT INTO agent_analysis_template_runs
                   (template_run_id,template_id,trigger,status,started_at,completed_at,error)
                   VALUES (?,?,?,?,?,?,?)""",
                (template_run_id, template_id, trigger, status, now_text, now_text if running else None, error),
            )
            conn.execute(
                """UPDATE agent_analysis_templates
                   SET next_run_at=?,last_run_at=?,last_status=?,updated_at=datetime('now','localtime')
                   WHERE template_id=?""",
                (next_run_at, now_text, status, template_id),
            )
            conn.commit()
            return _row(row), {
                "template_run_id": template_run_id, "template_id": template_id, "trigger": trigger,
                "status": status, "started_at": now_text, "completed_at": now_text if running else None,
                "error": error, "analysis_run_id": None,
            }
        finally:
            conn.close()

    def _finish_template_run(
        self,
        receipt: dict[str, Any],
        status: str,
        *,
        analysis_run_id: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        completed_at = _utc_text(_utc_now())
        conn = self._write_connection()
        try:
            conn.execute(
                """UPDATE agent_analysis_template_runs
                   SET analysis_run_id=?,status=?,completed_at=?,error=? WHERE template_run_id=?""",
                (analysis_run_id, status, completed_at, str(error or "")[:500] or None, receipt["template_run_id"]),
            )
            conn.execute(
                """UPDATE agent_analysis_templates
                   SET last_run_id=COALESCE(?,last_run_id),last_run_at=?,last_status=?,updated_at=datetime('now','localtime')
                   WHERE template_id=?""",
                (analysis_run_id, completed_at, status, receipt["template_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        return {**receipt, "analysis_run_id": analysis_run_id, "status": status, "completed_at": completed_at, "error": error}

    def run_template(self, template_id: str, *, trigger: str = "manual") -> dict[str, Any]:
        claimed = self._claim_template_run(template_id, trigger)
        if claimed is None:
            return {"ok": True, "skipped": True, "reason": "模板尚未到执行时间"}
        row, receipt = claimed
        if receipt["status"] == "skipped":
            if trigger == "manual":
                raise ValueError(str(receipt["error"]))
            return {"ok": True, "skipped": True, "template_run": receipt}
        try:
            run = self.create_run(str(row["catalog_id"]), str(row["question"]), json.loads(str(row["scope_json"])))
        except Exception as exc:
            failed = self._finish_template_run(receipt, "failed", error=str(exc))
            if trigger == "manual":
                raise
            return {"ok": False, "template_run": failed}
        result = dict(run.get("result") or {})
        status = "completed" if run.get("ok") else "failed"
        finished = self._finish_template_run(
            receipt, status, analysis_run_id=str(result.get("run_id") or "") or None,
            error=str((result.get("caveats") or [""])[0]) if status == "failed" else None,
        )
        return {**run, "template_run": finished}

    def run_due_templates(self, limit: int = 10) -> dict[str, Any]:
        self.recover_stale_template_runs()
        now_text = _utc_text(_utc_now())
        conn = self._write_connection()
        try:
            rows = conn.execute(
                """SELECT template_id FROM agent_analysis_templates
                   WHERE enabled=1 AND schedule_enabled=1 AND schedule_kind<>'manual'
                     AND next_run_at IS NOT NULL AND next_run_at<=?
                   ORDER BY next_run_at,id LIMIT ?""",
                (now_text, max(1, min(int(limit), 50))),
            ).fetchall()
        finally:
            conn.close()
        results = [self.run_template(str(row["template_id"]), trigger="schedule") for row in rows]
        return {"ok": True, "claimed": len(rows), "results": results}

    def list_template_runs(self, template_id: str, limit: int = 30) -> dict[str, Any]:
        conn = self._write_connection()
        try:
            exists = conn.execute(
                "SELECT 1 FROM agent_analysis_templates WHERE template_id=? AND enabled=1", (template_id,),
            ).fetchone()
            if not exists:
                raise LookupError(f"找不到固定分析：{template_id}")
            rows = conn.execute(
                """SELECT tr.*,ar.result_json FROM agent_analysis_template_runs tr
                   LEFT JOIN agent_analysis_runs ar ON ar.run_id=tr.analysis_run_id
                   WHERE tr.template_id=? ORDER BY tr.started_at DESC,tr.id DESC LIMIT ?""",
                (template_id, max(1, min(int(limit), 100))),
            ).fetchall()
        finally:
            conn.close()
        items = []
        for row in rows:
            result = json.loads(str(row["result_json"] or "{}"))
            items.append({
                "template_run_id": row["template_run_id"], "template_id": row["template_id"],
                "analysis_run_id": row["analysis_run_id"], "trigger": row["trigger"], "status": row["status"],
                "started_at": row["started_at"], "completed_at": row["completed_at"], "error": row["error"],
                "headline": result.get("headline"), "data_as_of": result.get("data_as_of"),
            })
        return {"ok": True, "items": items}

    def template_trend(self, template_id: str, limit: int = 30) -> dict[str, Any]:
        conn = self._write_connection()
        try:
            template = conn.execute(
                "SELECT name,catalog_id FROM agent_analysis_templates WHERE template_id=? AND enabled=1", (template_id,),
            ).fetchone()
            if not template:
                raise LookupError(f"找不到固定分析：{template_id}")
            rows = conn.execute(
                """SELECT tr.analysis_run_id,tr.started_at,ar.result_json
                   FROM agent_analysis_template_runs tr
                   JOIN agent_analysis_runs ar ON ar.run_id=tr.analysis_run_id
                   WHERE tr.template_id=? AND tr.status='completed' AND ar.status IN ('completed','partial')
                   ORDER BY tr.started_at DESC,tr.id DESC LIMIT ?""",
                (template_id, max(2, min(int(limit), 100))),
            ).fetchall()
        finally:
            conn.close()
        chronological = list(reversed(rows))
        metric_defs: dict[str, dict[str, Any]] = {}
        run_points: list[dict[str, Any]] = []
        for row in chronological:
            result = json.loads(str(row["result_json"] or "{}"))
            values: dict[str, int | float | None] = {}
            for metric in list(result.get("metrics") or []):
                metric_id = str(metric.get("id") or "")
                if not metric_id:
                    continue
                metric_defs.setdefault(metric_id, {
                    "metric_id": metric_id, "label": str(metric.get("label") or metric_id),
                    "unit": str(metric.get("unit") or "count"),
                })
                value = metric.get("value")
                values[metric_id] = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
            run_points.append({
                "run_id": row["analysis_run_id"], "at": result.get("data_as_of") or row["started_at"],
                "headline": result.get("headline"), "values": values,
            })
        series = []
        for metric_id, definition in metric_defs.items():
            points = [
                {"run_id": point["run_id"], "at": point["at"], "value": point["values"].get(metric_id)}
                for point in run_points
            ]
            numeric = [point["value"] for point in points if point["value"] is not None]
            latest = numeric[-1] if numeric else None
            previous = numeric[-2] if len(numeric) > 1 else None
            delta = latest - previous if latest is not None and previous is not None else None
            delta_ratio = round(delta / abs(previous), 4) if delta is not None and previous not in (None, 0) else None
            series.append({**definition, "latest": latest, "previous": previous, "delta": delta, "delta_ratio": delta_ratio, "points": points})
        return {
            "ok": True, "template_id": template_id, "name": template["name"],
            "catalog_id": template["catalog_id"], "run_count": len(run_points), "runs": run_points, "series": series,
        }

    def delete_template(self, template_id: str) -> dict[str, Any]:
        conn = self._write_connection()
        try:
            cursor = conn.execute(
                "UPDATE agent_analysis_templates SET enabled=0,updated_at=datetime('now','localtime') WHERE template_id=? AND enabled=1",
                (template_id,),
            )
            conn.commit()
        finally:
            conn.close()
        if not cursor.rowcount:
            raise LookupError(f"找不到固定分析：{template_id}")
        return {"ok": True, "template_id": template_id, "status": "deleted"}

    def export_run(self, run_id: str) -> dict[str, Any]:
        result = self.get_run(run_id)["result"]
        output_dir = self.db_path.parent / "analysis_reports"
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / f"{run_id}.md"
        lines = [f"# {result['headline']}", "", f"- 分析类型：{result['catalog_id']}", f"- 数据时间：{result['data_as_of']}", ""]
        for item in result.get("metrics", []):
            lines.append(f"- {item.get('label')}：{'数据不足' if item.get('value') is None else item.get('value')}")
        for section in result.get("sections", []):
            lines.extend(["", f"## {section.get('title')}", "", "```json", json.dumps(section.get("rows") or [], ensure_ascii=False, indent=2), "```"])
        target.write_text("\n".join(str(line) for line in lines) + "\n", encoding="utf-8")
        conn = self._write_connection()
        try:
            conn.execute("UPDATE agent_analysis_runs SET export_path=? WHERE run_id=?", (str(target), run_id))
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "artifact": {"schema_version": "artifact_envelope_v1", "artifact_type": "analysis_report", "artifact_id": f"analysis_report:{run_id}", "title": result["headline"], "download_url": f"/api/v1/analytics/runs/{run_id}/download", "source_run_id": run_id}}

    def export_file(self, run_id: str) -> Path:
        conn = self._write_connection()
        try:
            row = conn.execute(
                "SELECT export_path FROM agent_analysis_runs WHERE run_id=?", (run_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            raise LookupError(f"找不到分析记录：{run_id}")
        if not row["export_path"]:
            raise LookupError("分析报告尚未导出")
        root = (self.db_path.parent / "analysis_reports").resolve()
        target = Path(str(row["export_path"])).resolve()
        if target.parent != root or target.suffix != ".md" or not target.is_file():
            raise LookupError("分析报告文件不可用，请重新导出")
        return target

    def workbench(self, flow_inbox: dict[str, Any], limit: int = 100) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for source in list(flow_inbox.get("items") or []):
            source_id = str(source.get("job_candidate_id") or "")
            lane = _flow_item_lane(source)
            if not lane:
                continue
            revision = hashlib.sha256(
                _json({key: source.get(key) for key in ("queue", "updated_at", "signal", "priority_score")}).encode()
            ).hexdigest()[:16]
            items.append({
                "item_key": f"candidate:{source_id}", "source_revision": revision,
                "kind": "candidate_action", "lane": lane,
                "priority_score": int(source.get("priority_score") or 0),
                "title": str(source.get("candidate") or "待处理人选"),
                "subtitle": str(source.get("project") or ""),
                "status_label": str(source.get("queue") or "进行中"),
                "reason": str(source.get("signal") or ""), "source_label": "人选推进",
                "updated_at": source.get("updated_at") or source.get("last_event_time"),
                "primary_action": {"type": "open_candidate", "id": source_id, "label": str(source.get("next_action") or "查看")},
            })
        conn = self._write_connection()
        try:
            approvals = conn.execute(
                """SELECT a.approval_id,a.workflow_id,a.risk_level,a.title,a.created_at,w.updated_at,
                          g.title AS goal_title
                   FROM agent_approvals a JOIN agent_workflows w ON w.workflow_id=a.workflow_id
                   JOIN agent_goals g ON g.goal_id=a.goal_id
                   WHERE a.status='pending' ORDER BY a.created_at"""
            ).fetchall()
            runs = conn.execute(
                "SELECT result_json,created_at FROM agent_analysis_runs WHERE status IN ('completed','partial') ORDER BY id DESC LIMIT 6"
            ).fetchall()
            workflows = conn.execute(
                """SELECT w.workflow_id,w.status,w.current_stage,w.updated_at,g.title
                   FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
                   WHERE w.archived_at IS NULL AND w.status IN ('queued','running','waiting_external')
                   ORDER BY w.updated_at DESC LIMIT 12"""
            ).fetchall()
            states = {str(row["item_key"]): _row(row) for row in conn.execute("SELECT * FROM agent_inbox_state").fetchall()}
        finally:
            conn.close()
        for row in approvals:
            items.append({
                "item_key": f"approval:{row['approval_id']}",
                "source_revision": hashlib.sha256(f"{row['updated_at']}|{row['risk_level']}".encode()).hexdigest()[:16],
                "kind": "approval", "lane": "decision",
                "priority_score": 20_000 if str(row["risk_level"]) == "R3" else 8_000,
                "title": str(row["goal_title"] or row["title"]), "subtitle": str(row["title"] or "等待审批"),
                "status_label": f"{row['risk_level']} 待审批", "reason": "外部动作需由顾问单次确认",
                "source_label": "审批", "updated_at": row["created_at"],
                "primary_action": {"type": "open_workflow", "id": row["workflow_id"], "label": "查看并审批"},
            })
        for row in runs:
            result = json.loads(str(row["result_json"] or "{}"))
            run_id = str(result.get("run_id") or "")
            items.append({
                "item_key": f"analysis:{run_id}",
                "source_revision": hashlib.sha256(str(result.get("data_as_of") or "").encode()).hexdigest()[:16],
                "kind": "analysis", "lane": "delivered", "priority_score": 0,
                "title": str(result.get("headline") or "分析已完成"),
                "subtitle": CATALOGS.get(str(result.get("catalog_id")), {}).get("label", "分析结果"),
                "status_label": "已交付", "reason": "", "source_label": "分析",
                "updated_at": row["created_at"],
                "primary_action": {"type": "open_analysis", "id": run_id, "label": "查看分析"},
            })
        for row in workflows:
            workflow_id = str(row["workflow_id"] or "")
            items.append({
                "item_key": f"workflow:{workflow_id}",
                "source_revision": hashlib.sha256(
                    f"{row['status']}|{row['current_stage']}|{row['updated_at']}".encode()
                ).hexdigest()[:16],
                "kind": "workflow", "lane": "running", "priority_score": 6_000,
                "title": str(row["title"] or "Agent 工作流"),
                "subtitle": str(row["current_stage"] or "正在执行"),
                "status_label": "等待渠道回执" if row["status"] == "waiting_external" else "运行中",
                "reason": "", "source_label": "Agent 任务", "updated_at": row["updated_at"],
                "primary_action": {"type": "open_workflow", "id": workflow_id, "label": "查看进度"},
            })
        visible = []
        for item in items:
            state = states.get(str(item["item_key"]), {})
            if state.get("state") == "hidden" and state.get("source_revision") == item["source_revision"]:
                continue
            item["inbox_state"] = state.get("state") or "unread"
            visible.append(item)
        visible.sort(key=lambda item: (-int(item["priority_score"]), str(item.get("updated_at") or "")))
        lanes = {lane: sum(item["lane"] == lane for item in visible) for lane in WORKBENCH_LANES}
        max_items = max(1, min(int(limit), 300))
        selected = visible[:max_items]
        # 大量候选待办不能把运行中任务、风险项和最新交付完全挤出工作台。
        for lane in ("running", "waiting_client", "risk", "delivered"):
            representative = next((item for item in visible if item["lane"] == lane), None)
            if representative and not any(item["lane"] == lane for item in selected):
                replace_at = next(
                    (index for index in range(len(selected) - 1, -1, -1) if selected[index]["lane"] == "decision"),
                    None,
                )
                if replace_at is not None:
                    selected[replace_at] = representative
        selected.sort(key=lambda item: (-int(item["priority_score"]), str(item.get("updated_at") or "")))
        # pending 保留为 decision 的兼容别名（既有调用方只读 pending/running/delivered/total）。
        summary = {**lanes, "pending": lanes["decision"], "total": len(visible)}
        version = hashlib.sha256(
            _json({"items": [(item["item_key"], item["source_revision"]) for item in selected], "summary": summary}).encode()
        ).hexdigest()[:16]
        return {
            "ok": True, "version": version, "summary": summary, "items": selected,
            "returned_count": len(selected), "truncated": len(selected) < len(visible),
        }

    def set_inbox_state(self, item_key: str, state: str, source_revision: str = "") -> dict[str, Any]:
        if state not in {"unread", "read", "later", "hidden"}:
            raise ValueError("未知收件箱状态")
        conn = self._write_connection()
        try:
            conn.execute(
                """INSERT INTO agent_inbox_state(item_key,state,source_revision) VALUES (?,?,?)
                   ON CONFLICT(item_key) DO UPDATE SET state=excluded.state,
                       source_revision=excluded.source_revision,updated_at=datetime('now','localtime')""",
                (item_key, state, source_revision),
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "item_key": item_key, "state": state}

    def _operations_overview(self, conn: sqlite3.Connection, scope: dict[str, Any]) -> dict[str, Any]:
        active_jobs = int(conn.execute(
            f"SELECT COUNT(*) FROM jobs j WHERE {ACTIVE_JOB_PREDICATE}"
        ).fetchone()[0])
        active_candidates = int(conn.execute(
            f"""SELECT COUNT(*) FROM job_candidates jc JOIN jobs j ON j.id=jc.job_id
                WHERE {ACTIVE_JOB_PREDICATE}
                  AND COALESCE(jc.clean_stage,'') NOT LIKE 'H5 %'
                  AND lower(COALESCE(jc.raw_status,'')) NOT IN
                      ('screen_rejected','rejected','client_rejected','eliminated','closed','stopped')"""
        ).fetchone()[0])
        pending = int(conn.execute(
            f"""SELECT COUNT(*) FROM job_candidates jc JOIN jobs j ON j.id=jc.job_id
                WHERE {ACTIVE_JOB_PREDICATE}
                  AND COALESCE(jc.clean_stage,'') NOT LIKE 'H5 %'
                  AND lower(COALESCE(jc.raw_status,'')) NOT IN
                      ('screen_rejected','rejected','client_rejected','eliminated','closed','stopped')
                  AND (jc.clean_stage LIKE '%待%' OR jc.clean_stage LIKE 'H1 %' OR jc.clean_stage LIKE 'X1 %')"""
        ).fetchone()[0])
        p0 = int(conn.execute(
            f"""SELECT COUNT(*) FROM job_pipeline_metrics m JOIN jobs j ON j.id=m.job_id
                WHERE {ACTIVE_JOB_PREDICATE}
                  AND m.id=(SELECT MAX(x.id) FROM job_pipeline_metrics x WHERE x.job_id=m.job_id)
                  AND COALESCE(m.priority,'') LIKE 'P0%'"""
        ).fetchone()[0])
        rows = conn.execute(
            f"""SELECT j.id,cl.name AS client,j.title,COUNT(jc.id) AS candidates,
                       SUM(CASE WHEN jc.id IS NULL
                                 OR COALESCE(jc.clean_stage,'') LIKE 'H5 %'
                                 OR lower(COALESCE(jc.raw_status,'')) IN
                                    ('screen_rejected','rejected','client_rejected','eliminated','closed','stopped')
                                THEN 0 ELSE 1 END) AS active_candidates
                FROM jobs j JOIN clients cl ON cl.id=j.client_id LEFT JOIN job_candidates jc ON jc.job_id=j.id
                WHERE {ACTIVE_JOB_PREDICATE}
                GROUP BY j.id ORDER BY active_candidates ASC,j.updated_at DESC LIMIT 10"""
        ).fetchall()
        return {
            "headline": f"{pending} 项人选推进需要关注，{p0} 个 P0 岗位优先",
            "metrics": [_metric("active_jobs", "开放岗位", active_jobs), _metric("active_candidates", "有效人选", active_candidates), _metric("pending_candidates", "待处理人选", pending), _metric("p0_jobs", "P0 岗位", p0)],
            "sections": [{"type": "table", "title": "岗位关注顺序", "columns": ["client", "title", "active_candidates", "candidates"], "rows": [_row(row) for row in rows]}],
            "references": [{"type": "job", "id": row["id"], "label": f"{row['client']} / {row['title']}", "href": f"#job={row['id']}"} for row in rows],
            "caveats": [],
        }

    def _job_health(self, conn: sqlite3.Connection, scope: dict[str, Any]) -> dict[str, Any]:
        params: list[Any] = []
        where = "WHERE j.id=?" if scope.get("job_id") else f"WHERE {ACTIVE_JOB_PREDICATE}"
        if scope.get("job_id"):
            params.append(scope["job_id"])
        rows = conn.execute(
            f"""SELECT j.id,cl.name AS client,j.title,COUNT(jc.id) AS total,
                       SUM(CASE WHEN COALESCE(jc.clean_stage,'') LIKE 'H5 %' OR lower(COALESCE(jc.raw_status,'')) IN ('screen_rejected','rejected','client_rejected','eliminated','closed','stopped') THEN 1 ELSE 0 END) AS stopped,
                       SUM(CASE WHEN jc.clean_stage LIKE '%触达%' OR jc.clean_stage LIKE '%联系%' THEN 1 ELSE 0 END) AS contacted,
                       SUM(CASE WHEN jc.clean_stage LIKE '%推荐%' THEN 1 ELSE 0 END) AS recommended
                FROM jobs j JOIN clients cl ON cl.id=j.client_id LEFT JOIN job_candidates jc ON jc.job_id=j.id
                {where} GROUP BY j.id ORDER BY total DESC LIMIT 10""",
            params,
        ).fetchall()
        data = []
        for row in rows:
            item = _row(row)
            item["active"] = int(item["total"] or 0) - int(item["stopped"] or 0)
            item["recommendation_rate"] = _ratio(int(item["recommended"] or 0), int(item["total"] or 0))
            data.append(item)
        total = sum(int(item["total"] or 0) for item in data)
        active = sum(int(item["active"] or 0) for item in data)
        return {
            "headline": f"{len(data)} 个岗位共有 {active} 名有效人选",
            "metrics": [_metric("jobs_in_scope", "范围内岗位", len(data)), _metric("pipeline_total", "管道人选", total), _metric("pipeline_active", "有效人选", active), _metric("active_rate", "有效率", _ratio(active, total), "ratio")],
            "sections": [{"type": "table", "title": "岗位漏斗", "columns": ["client", "title", "total", "active", "contacted", "recommended"], "rows": data}],
            "references": [{"type": "job", "id": item["id"], "label": f"{item['client']} / {item['title']}", "href": f"#job={item['id']}"} for item in data],
            "caveats": ["有效率分母为零时返回 null。"] if total == 0 else [],
        }

    def _talent_search(self, conn: sqlite3.Connection, scope: dict[str, Any]) -> dict[str, Any]:
        conditions = [
            "COALESCE(jc.clean_stage,'') NOT LIKE 'H5 %'",
            "lower(COALESCE(jc.raw_status,'')) NOT IN ('screen_rejected','rejected','client_rejected','eliminated','closed','stopped')",
        ]
        params: list[Any] = []
        fields = {"job_id": "jc.job_id=?", "company": "p.current_company LIKE ?", "title": "p.current_title LIKE ?", "city": "p.city LIKE ?", "stage": "jc.clean_stage=?"}
        for key, clause in fields.items():
            if key not in scope:
                continue
            conditions.append(clause)
            params.append(scope[key] if key in {"job_id", "stage"} else f"%{scope[key]}%")
        limit = int(scope.get("limit", MAX_RESULT_ROWS))
        rows = conn.execute(
            f"""SELECT jc.id,p.display_name AS name,p.current_company AS company,p.current_title AS title,p.city,
                       jc.clean_stage,j.id AS job_id,j.title AS job,cl.name AS client
                FROM job_candidates jc JOIN people p ON p.id=jc.person_id
                LEFT JOIN jobs j ON j.id=jc.job_id LEFT JOIN clients cl ON cl.id=j.client_id
                WHERE {' AND '.join(conditions)}
                ORDER BY COALESCE(jc.updated_at,'') DESC,jc.id DESC LIMIT ?""",
            [*params, limit],
        ).fetchall()
        data = [_row(row) for row in rows]
        return {
            "headline": f"找到 {len(data)} 名符合结构化条件的有效人选",
            "metrics": [_metric("matched_candidates", "匹配人选", len(data))],
            "sections": [{"type": "candidate_list", "title": "候选人", "columns": ["name", "company", "title", "city", "client", "job", "clean_stage"], "rows": data}],
            "references": [{"type": "candidate", "id": item["id"], "label": str(item["name"]), "href": f"#candidate={item['id']}"} for item in data],
            "caveats": ["仅返回结构化字段，不包含完整简历。"], "truncated": len(data) >= limit,
        }

    def _candidate_compare(self, conn: sqlite3.Connection, scope: dict[str, Any]) -> dict[str, Any]:
        ids = scope.get("candidate_ids") or []
        rows = conn.execute(
            f"""SELECT jc.id,p.display_name AS name,p.current_company AS company,p.current_title AS title,p.city,
                       jc.clean_stage,j.title AS job,cl.name AS client,a.fit_score,a.fit_level,
                       a.recommendation,a.evidence_coverage
                FROM job_candidates jc JOIN people p ON p.id=jc.person_id
                LEFT JOIN jobs j ON j.id=jc.job_id LEFT JOIN clients cl ON cl.id=j.client_id
                LEFT JOIN agent_candidate_assessments a ON a.id=(SELECT MAX(a2.id) FROM agent_candidate_assessments a2 WHERE a2.job_candidate_id=jc.id AND a2.is_current=1)
                WHERE jc.id IN ({','.join('?' for _ in ids)}) ORDER BY COALESCE(a.fit_score,-1) DESC,jc.id""",
            ids,
        ).fetchall()
        data = [_row(row) for row in rows]
        scores = [int(item["fit_score"]) for item in data if item.get("fit_score") is not None]
        return {
            "headline": f"已按统一证据口径对比 {len(data)} 名人选",
            "metrics": [_metric("compared_candidates", "对比人选", len(data)), _metric("average_fit_score", "平均匹配分", round(sum(scores) / len(scores), 1) if scores else None, "score")],
            "sections": [{"type": "table", "title": "人选对比", "columns": ["name", "company", "title", "fit_score", "fit_level", "recommendation", "evidence_coverage"], "rows": data}],
            "references": [{"type": "candidate", "id": item["id"], "label": str(item["name"]), "href": f"#candidate={item['id']}"} for item in data],
            "caveats": ["未生成评估的人选分数保留为空，不以零分替代。"],
        }

    def _channel_performance(self, conn: sqlite3.Connection, scope: dict[str, Any]) -> dict[str, Any]:
        days = int(scope.get("days", 30))
        conditions, params = ["created_at>=datetime('now','localtime',?)"], [f"-{days} days"]
        if scope.get("job_id"):
            conditions.append("job_id=?")
            params.append(scope["job_id"])
        rows = conn.execute(
            f"""SELECT channel,SUM(query_count) AS queries,SUM(recall_count) AS recalled,
                       SUM(intake_new_count) AS intaked,SUM(assessed_count) AS assessed,SUM(high_score_count) AS high_score
                FROM agent_sourcing_funnel WHERE {' AND '.join(conditions)}
                GROUP BY channel ORDER BY recalled DESC""",
            params,
        ).fetchall()
        data = []
        for row in rows:
            item = _row(row)
            item["intake_rate"] = _ratio(int(item["intaked"] or 0), int(item["recalled"] or 0))
            item["high_score_rate"] = _ratio(int(item["high_score"] or 0), int(item["assessed"] or 0))
            data.append(item)
        recalled = sum(int(item["recalled"] or 0) for item in data)
        intaked = sum(int(item["intaked"] or 0) for item in data)
        return {
            "headline": f"最近 {days} 天共召回 {recalled} 人，新增入库 {intaked} 人",
            "metrics": [_metric("channel_recalled", "渠道召回", recalled), _metric("channel_intaked", "新增入库", intaked), _metric("channel_intake_rate", "入库率", _ratio(intaked, recalled), "ratio")],
            "sections": [{"type": "bar", "title": "渠道效果", "columns": ["channel", "recalled", "intaked", "assessed", "high_score", "intake_rate"], "rows": data}],
            "references": [], "caveats": ["无召回时入库率为 null。"] if not recalled else [],
        }

    def _workflow_funnel(self, conn: sqlite3.Connection, scope: dict[str, Any]) -> dict[str, Any]:
        workflow_id = scope.get("workflow_id")
        rows = conn.execute(
            f"SELECT s.status,COUNT(*) AS total FROM agent_workflow_steps s {'WHERE s.workflow_id=?' if workflow_id else ''} GROUP BY s.status ORDER BY total DESC",
            [workflow_id] if workflow_id else [],
        ).fetchall()
        data = [_row(row) for row in rows]
        total = sum(int(item["total"]) for item in data)
        completed = sum(int(item["total"]) for item in data if item["status"] == "completed")
        return {
            "headline": f"{completed}/{total} 个工作流步骤已完成" if total else "当前范围没有工作流步骤",
            "metrics": [_metric("workflow_steps", "步骤总数", total), _metric("workflow_completed", "已完成", completed), _metric("workflow_completion_rate", "完成率", _ratio(completed, total), "ratio")],
            "sections": [{"type": "funnel", "title": "步骤状态", "columns": ["status", "total"], "rows": data}],
            "references": [{"type": "workflow", "id": workflow_id, "label": "工作流", "href": f"#workflow={workflow_id}"}] if workflow_id else [],
            "caveats": ["无步骤时完成率为 null。"] if not total else [],
        }

    def _data_quality(self, conn: sqlite3.Connection, scope: dict[str, Any]) -> dict[str, Any]:
        missing_job = int(conn.execute("SELECT COUNT(*) FROM job_candidates WHERE job_id IS NULL").fetchone()[0])
        missing_stage = int(conn.execute("SELECT COUNT(*) FROM job_candidates WHERE trim(COALESCE(clean_stage,''))='' ").fetchone()[0])
        missing_profile = int(conn.execute("SELECT COUNT(*) FROM people WHERE trim(COALESCE(current_company,''))='' OR trim(COALESCE(current_title,''))='' ").fetchone()[0])
        duplicates = int(conn.execute("SELECT COALESCE(SUM(total-1),0) FROM (SELECT COUNT(*) AS total FROM job_candidates GROUP BY job_id,person_id HAVING COUNT(*)>1)").fetchone()[0])
        issues = [
            {"issue": "未关联岗位", "count": missing_job, "severity": "high"},
            {"issue": "缺少规范阶段", "count": missing_stage, "severity": "high"},
            {"issue": "公司或职位缺失", "count": missing_profile, "severity": "medium"},
            {"issue": "重复人岗关系", "count": duplicates, "severity": "medium"},
        ]
        total = sum(item["count"] for item in issues)
        return {
            "headline": f"检测到 {total} 项待治理数据问题",
            "metrics": [_metric("data_quality_issues", "问题总数", total), _metric("missing_job", "未关联岗位", missing_job), _metric("missing_stage", "缺少阶段", missing_stage), _metric("duplicate_relations", "重复关系", duplicates)],
            "sections": [{"type": "table", "title": "质量问题", "columns": ["issue", "count", "severity"], "rows": issues}],
            "references": [], "caveats": ["本分析只读，不自动合并或修复数据。"],
        }

    # 面试信号：推荐包反馈 interview，或客户反馈事件进入面试阶段（只计确认推荐之后的时间）。
    _INTERVIEW_EVENT_STATUSES = ("interview", "interviewing", "interview_passed")

    def _delivery_scorecard(self, conn: sqlite3.Connection, scope: dict[str, Any]) -> dict[str, Any]:
        """交付记分卡：固定分析的 5 个核心指标（只读、确定性计算，scope 支持 job_id/days）。

        口径（与各项指标 note 一致）：
        1. 有效推荐率 = 顾问确认推荐数 / 已完成评估人数，与
           service.consultant_recommendation_metrics 同口径：分子
           consultant_confirmed_recommendations 行数（表级 UNIQUE(job_candidate_id)）；
           分母 agent_candidate_assessments(is_current=1 且 agent_runs.status='completed')
           的去重 job_candidate_id 数。
        2. 推荐至面试转化 = 确认推荐后出现面试信号的人数 / 确认推荐人数。面试信号 =
           recommendation_package_feedback.feedback_type='interview'，或 candidate_events
           event_type='client_feedback' 且 event_status IN ('interview','interviewing','interview_passed')
           （反馈落库时 event_status=feedback_type，故两路都要覆盖）；仅计 confirmed_at 之后的记录。
        3. 渠道质量 = 复用 channel_performance 口径（agent_sourcing_funnel 按天窗口聚合）：
           入库率 = SUM(intake_new_count)/SUM(recall_count)，高分率 = SUM(high_score_count)/SUM(assessed_count)。
        4. 岗位关闭周期 = julianday(jobs.closed_at)-julianday(jobs.created_at) 的天数分布
           （中位数/平均）。jobs 表没有单独的“已关闭”状态枚举，关闭以 closed_at 落库为准
           （lifecycle_stage='archived' 但 closed_at 为空的岗位不计入）。
        5. 复盘完成率 = 有策略复盘的终局寻访工作流 / 终局寻访工作流。寻访工作流 = 存在
           search_strategy artifact 或 agent_sourcing_funnel 记录的 agent_workflows；终局 =
           status IN ('completed','blocked','failed')（与 strategy_review.TERMINAL_STATUSES 一致）；
           已复盘 = 存在 artifact_type='strategy_review' 的 agent_artifacts 记录。
        """
        days = int(scope.get("days", 30))
        job_id = scope.get("job_id")
        job_clause = "AND j.id=?" if job_id else ""
        job_params: list[Any] = [job_id] if job_id else []

        # ---- 1+2. 有效推荐率 & 推荐至面试转化（按岗位明细 + 全库汇总）----
        interview_exists = f"""EXISTS (
                SELECT 1 FROM recommendation_package_feedback f
                WHERE f.job_candidate_id=c.job_candidate_id AND f.feedback_type='interview'
                  AND f.feedback_time>=c.confirmed_at
            ) OR EXISTS (
                SELECT 1 FROM candidate_events e
                WHERE e.job_candidate_id=c.job_candidate_id AND e.event_type='client_feedback'
                  AND e.event_status IN ({','.join('?' for _ in self._INTERVIEW_EVENT_STATUSES)})
                  AND e.event_time>=c.confirmed_at
            )"""
        confirmed_total = int(conn.execute(
            f"SELECT COUNT(*) FROM consultant_confirmed_recommendations c WHERE 1=1 {'AND c.job_id=?' if job_id else ''}",
            job_params,
        ).fetchone()[0])
        assessed_total = int(conn.execute(
            f"""SELECT COUNT(DISTINCT a.job_candidate_id)
                FROM agent_candidate_assessments a
                JOIN agent_runs r ON r.run_id=a.run_id
                JOIN job_candidates jc ON jc.id=a.job_candidate_id
                WHERE a.is_current=1 AND r.status='completed' {'AND jc.job_id=?' if job_id else ''}""",
            job_params,
        ).fetchone()[0])
        interviewed_total = int(conn.execute(
            f"SELECT COUNT(*) FROM consultant_confirmed_recommendations c WHERE ({interview_exists}) {'AND c.job_id=?' if job_id else ''}",
            [*self._INTERVIEW_EVENT_STATUSES, *job_params],
        ).fetchone()[0])
        job_rows = [
            {
                **_row(row),
                "recommendation_rate": _ratio(int(row["confirmed"] or 0), int(row["assessed"] or 0)),
                "interview_rate": _ratio(int(row["interviewed"] or 0), int(row["confirmed"] or 0)),
            }
            for row in conn.execute(
                f"""SELECT j.id,cl.name AS client,j.title,
                           COALESCE(a.assessed,0) AS assessed,
                           COALESCE(c.confirmed,0) AS confirmed,
                           COALESCE(i.interviewed,0) AS interviewed
                    FROM jobs j JOIN clients cl ON cl.id=j.client_id
                    LEFT JOIN (
                        SELECT jc.job_id,COUNT(DISTINCT a.job_candidate_id) AS assessed
                        FROM agent_candidate_assessments a
                        JOIN agent_runs r ON r.run_id=a.run_id
                        JOIN job_candidates jc ON jc.id=a.job_candidate_id
                        WHERE a.is_current=1 AND r.status='completed' GROUP BY jc.job_id
                    ) a ON a.job_id=j.id
                    LEFT JOIN (
                        SELECT job_id,COUNT(*) AS confirmed
                        FROM consultant_confirmed_recommendations GROUP BY job_id
                    ) c ON c.job_id=j.id
                    LEFT JOIN (
                        SELECT c.job_id,COUNT(*) AS interviewed
                        FROM consultant_confirmed_recommendations c
                        WHERE {interview_exists} GROUP BY c.job_id
                    ) i ON i.job_id=j.id
                    WHERE (COALESCE(a.assessed,0)>0 OR COALESCE(c.confirmed,0)>0) {job_clause}
                    ORDER BY confirmed DESC,assessed DESC,j.id LIMIT {MAX_RESULT_ROWS}""",
                [*self._INTERVIEW_EVENT_STATUSES, *job_params],
            ).fetchall()
        ]

        # ---- 3. 渠道质量（复用 channel_performance 口径）----
        channel_conditions, channel_params = ["created_at>=datetime('now','localtime',?)"], [f"-{days} days"]
        if job_id:
            channel_conditions.append("job_id=?")
            channel_params.append(job_id)
        channel_rows = []
        for row in conn.execute(
            f"""SELECT channel,SUM(query_count) AS queries,SUM(recall_count) AS recalled,
                       SUM(intake_new_count) AS intaked,SUM(assessed_count) AS assessed,SUM(high_score_count) AS high_score
                FROM agent_sourcing_funnel WHERE {' AND '.join(channel_conditions)}
                GROUP BY channel ORDER BY recalled DESC""",
            channel_params,
        ).fetchall():
            item = _row(row)
            item["intake_rate"] = _ratio(int(item["intaked"] or 0), int(item["recalled"] or 0))
            item["high_score_rate"] = _ratio(int(item["high_score"] or 0), int(item["assessed"] or 0))
            channel_rows.append(item)
        recalled = sum(int(item["recalled"] or 0) for item in channel_rows)
        intaked = sum(int(item["intaked"] or 0) for item in channel_rows)
        channel_assessed = sum(int(item["assessed"] or 0) for item in channel_rows)
        high_score = sum(int(item["high_score"] or 0) for item in channel_rows)

        # ---- 4. 岗位关闭周期（closed_at 落库为准）----
        closure_rows = [
            _row(row)
            for row in conn.execute(
                f"""SELECT j.id,cl.name AS client,j.title,j.created_at,j.closed_at,
                           ROUND(julianday(j.closed_at)-julianday(j.created_at),1) AS closure_days
                    FROM jobs j JOIN clients cl ON cl.id=j.client_id
                    WHERE j.closed_at IS NOT NULL AND j.created_at IS NOT NULL
                      AND julianday(j.closed_at)>=julianday(j.created_at) {job_clause}
                    ORDER BY closure_days DESC,j.id""",
                job_params,
            ).fetchall()
        ]
        closure_days = sorted(float(item["closure_days"]) for item in closure_rows)
        closure_median: float | None = None
        closure_avg: float | None = None
        if closure_days:
            middle = len(closure_days) // 2
            closure_median = round(
                closure_days[middle] if len(closure_days) % 2 else (closure_days[middle - 1] + closure_days[middle]) / 2, 1,
            )
            closure_avg = round(sum(closure_days) / len(closure_days), 1)

        # ---- 5. 复盘完成率（终局寻访工作流中有 strategy_review 复盘的比例）----
        review_clause = "AND g.context_type='job' AND g.context_id=?" if job_id else ""
        review_rows = [
            _row(row)
            for row in conn.execute(
                f"""SELECT w.workflow_id,g.title AS workflow_title,w.status,w.updated_at,
                           CASE WHEN EXISTS (
                               SELECT 1 FROM agent_artifacts ra
                               WHERE ra.workflow_id=w.workflow_id AND ra.artifact_type='strategy_review'
                           ) THEN '已复盘' ELSE '未复盘' END AS review_state
                    FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
                    WHERE w.status IN ('completed','blocked','failed')
                      AND (EXISTS (
                               SELECT 1 FROM agent_artifacts sa
                               WHERE sa.workflow_id=w.workflow_id AND sa.artifact_type='search_strategy'
                           ) OR EXISTS (
                               SELECT 1 FROM agent_sourcing_funnel sf WHERE sf.workflow_id=w.workflow_id
                           )) {review_clause}
                    ORDER BY w.updated_at DESC,w.workflow_id""",
                job_params,
            ).fetchall()
        ]
        terminal = len(review_rows)
        reviewed = sum(1 for item in review_rows if item["review_state"] == "已复盘")

        metrics = [
            _scorecard_metric(
                "effective_recommendation_rate", "有效推荐率",
                _ratio(confirmed_total, assessed_total), "ratio", assessed_total,
                "顾问确认推荐 ÷ 已完成评估人数（评估 is_current 且对应 run 已完成）",
            ),
            _scorecard_metric(
                "recommendation_to_interview_rate", "推荐至面试转化",
                _ratio(interviewed_total, confirmed_total), "ratio", confirmed_total,
                "确认推荐后出现面试信号（推荐包反馈 interview 或客户反馈事件进面试）÷ 确认推荐人数",
            ),
            _scorecard_metric(
                "channel_intake_rate", "渠道入库率",
                _ratio(intaked, recalled), "ratio", recalled,
                f"最近 {days} 天新增入库 ÷ 渠道召回（agent_sourcing_funnel 按渠道聚合）",
            ),
            _scorecard_metric(
                "channel_high_score_rate", "渠道高分率",
                _ratio(high_score, channel_assessed), "ratio", channel_assessed,
                f"最近 {days} 天高分人选 ÷ 已评估（与渠道入库率同一窗口）",
            ),
            _scorecard_metric(
                "job_closure_days_median", "关闭周期中位数",
                closure_median, "days", len(closure_rows),
                "岗位 closed_at − created_at 的天数；仅统计已落关闭时间的岗位",
            ),
            _scorecard_metric(
                "job_closure_days_avg", "关闭周期平均",
                closure_avg, "days", len(closure_rows),
                "岗位 closed_at − created_at 的天数；仅统计已落关闭时间的岗位",
            ),
            _scorecard_metric(
                "strategy_review_completion_rate", "复盘完成率",
                _ratio(reviewed, terminal), "ratio", terminal,
                "有策略复盘的终局寻访工作流 ÷ 终局寻访工作流（completed/blocked/failed）",
            ),
        ]

        caveats = []
        if not assessed_total:
            caveats.append("无已完成评估人选，有效推荐率为 null（样本量 0）。")
        if not confirmed_total:
            caveats.append("无顾问确认推荐，推荐至面试转化为 null（样本量 0）。")
        if not recalled:
            caveats.append(f"最近 {days} 天无渠道召回，渠道入库率为 null。")
        if not channel_assessed:
            caveats.append(f"最近 {days} 天无已评估人选，渠道高分率为 null。")
        if not closure_rows:
            caveats.append("当前范围没有已关闭岗位（closed_at 为空），关闭周期为 null。")
        if not terminal:
            caveats.append("没有终局寻访工作流，复盘完成率为 null（样本量 0）。")

        def _fmt_ratio(value: float | None) -> str:
            return "数据不足" if value is None else f"{round(value * 1000) / 10}%"

        headline = (
            f"有效推荐率 {_fmt_ratio(metrics[0]['value'])}（样本 {assessed_total}），"
            f"推荐至面试 {_fmt_ratio(metrics[1]['value'])}（样本 {confirmed_total}），"
            f"复盘完成率 {_fmt_ratio(metrics[6]['value'])}（样本 {terminal}）"
        )
        return {
            "headline": headline,
            "metrics": metrics,
            "sections": [
                {"type": "table", "title": "岗位推荐明细", "columns": ["client", "title", "assessed", "confirmed", "recommendation_rate", "interviewed", "interview_rate"], "rows": job_rows},
                {"type": "bar", "title": "渠道质量", "columns": ["channel", "recalled", "intaked", "assessed", "high_score", "intake_rate", "high_score_rate"], "rows": channel_rows[:MAX_RESULT_ROWS]},
                {"type": "table", "title": "岗位关闭周期", "columns": ["client", "title", "created_at", "closed_at", "closure_days"], "rows": closure_rows[:MAX_RESULT_ROWS]},
                {"type": "table", "title": "寻访工作流复盘", "columns": ["workflow_id", "workflow_title", "status", "review_state"], "rows": review_rows[:MAX_RESULT_ROWS]},
            ],
            "references": [
                *[{"type": "job", "id": item["id"], "label": f"{item['client']} / {item['title']}", "href": f"#job={item['id']}"} for item in job_rows],
                *[{"type": "workflow", "id": item["workflow_id"], "label": str(item["workflow_title"]), "href": f"#workflow={item['workflow_id']}"} for item in review_rows[:MAX_RESULT_ROWS]],
            ],
            "caveats": caveats,
            "truncated": len(closure_rows) > MAX_RESULT_ROWS or len(review_rows) > MAX_RESULT_ROWS or len(channel_rows) > MAX_RESULT_ROWS,
        }
