"""ASA 内建定时任务调度器（纯 threading，无第三方依赖）。

后台线程每 30 秒检查一次到期任务并执行。任务持久化在 SQLite 的
``scheduled_tasks`` 表中，cron 表达式为简化的 5 段格式 "分 时 日 月 周"，
每段支持 ``*`` 或数字（也兼容逗号分隔的数字列表）。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 30
# _next_run_after 最多向前扫描的天数
_MAX_SCAN_DAYS = 366

ExecutorFn = Callable[["Scheduler", dict[str, Any]], Any]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    task_type TEXT NOT NULL,
    cron_expr TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    next_run_at TEXT,
    created_at TEXT NOT NULL
)
"""


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class Scheduler:
    """基于 threading 的轻量定时任务调度器。"""

    def __init__(self, db_path: str | Path, executor_registry: dict[str, ExecutorFn] | None = None):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._executors: dict[str, ExecutorFn] = {
            "db_maintenance": Scheduler._exec_db_maintenance,
            "followup_reminder": Scheduler._exec_followup_reminder,
        }
        if executor_registry:
            self._executors.update(executor_registry)
        self._init_db()

    # ------------------------------------------------------------------ db

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(_SCHEMA)

    # -------------------------------------------------------------- cron

    @staticmethod
    def _field_matches(field: str, value: int) -> bool:
        field = field.strip()
        if field == "*":
            return True
        for part in field.split(","):
            part = part.strip()
            if part.isdigit() and int(part) == value:
                return True
        return False

    def _parse_cron(self, cron_expr: str, now: datetime) -> bool:
        """判断 now 是否匹配 cron 表达式（"分 时 日 月 周"，支持 * 和数字）。

        周几按 cron 惯例：0 和 7 都表示周日。日与周同时限制时要求两者都匹配。
        """
        parts = cron_expr.split()
        if len(parts) != 5:
            return False
        minute, hour, dom, month, dow = parts
        cron_dow = (now.weekday() + 1) % 7  # Monday=1 ... Saturday=6, Sunday=0
        return (
            self._field_matches(minute, now.minute)
            and self._field_matches(hour, now.hour)
            and self._field_matches(dom, now.day)
            and self._field_matches(month, now.month)
            and (self._field_matches(dow, cron_dow) or (cron_dow == 0 and self._field_matches(dow, 7)))
        )

    def _next_run_after(self, cron_expr: str, from_dt: datetime) -> datetime | None:
        """从 from_dt 之后逐分钟扫描，找到下一个匹配时间。"""
        candidate = from_dt.replace(second=0) + timedelta(minutes=1)
        limit = from_dt + timedelta(days=_MAX_SCAN_DAYS)
        while candidate <= limit:
            if self._parse_cron(cron_expr, candidate):
                return candidate
            candidate += timedelta(minutes=1)
        return None

    # ----------------------------------------------------------- lifecycle

    def start(self) -> None:
        """启动后台线程，每 30 秒检查一次到期任务。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="asa-scheduler", daemon=True)
        self._thread.start()
        logger.info("Scheduler started (db=%s)", self.db_path)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=CHECK_INTERVAL_SECONDS + 5)
            self._thread = None
        logger.info("Scheduler stopped")

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._run_due_tasks()
            except Exception:
                logger.exception("Scheduler tick failed")
            self._stop_event.wait(CHECK_INTERVAL_SECONDS)

    def _run_due_tasks(self) -> None:
        now_str = _fmt(_now())
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?",
                (now_str,),
            ).fetchall()
        for row in rows:
            self._execute(dict(row))

    # ------------------------------------------------------------- CRUD

    def create_task(self, name: str, task_type: str, cron_expr: str, params: dict | None = None) -> int:
        now = _now()
        next_run = self._next_run_after(cron_expr, now)
        if next_run is None:
            raise ValueError(f"无法解析 cron 表达式或一年内无匹配时间: {cron_expr!r}")
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO scheduled_tasks (name, task_type, cron_expr, params_json, enabled, next_run_at, created_at)"
                " VALUES (?, ?, ?, ?, 1, ?, ?)",
                (name, task_type, cron_expr, json.dumps(params or {}, ensure_ascii=False), _fmt(next_run), _fmt(now)),
            )
            task_id = int(cur.lastrowid)
        logger.info("Created task #%s (%s, %s), next run %s", task_id, name, cron_expr, _fmt(next_run))
        return task_id

    def pause_task(self, task_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE scheduled_tasks SET enabled = 0 WHERE id = ?", (task_id,))

    def resume_task(self, task_id: int) -> None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT cron_expr FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"task #{task_id} 不存在")
            next_run = self._next_run_after(row["cron_expr"], _now())
            conn.execute(
                "UPDATE scheduled_tasks SET enabled = 1, next_run_at = ? WHERE id = ?",
                (_fmt(next_run) if next_run else None, task_id),
            )

    def delete_task(self, task_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))

    def list_tasks(self) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM scheduled_tasks ORDER BY id").fetchall()
        tasks = []
        for row in rows:
            task = dict(row)
            try:
                task["params"] = json.loads(task.pop("params_json") or "{}")
            except json.JSONDecodeError:
                task["params"] = {}
            tasks.append(task)
        return tasks

    # ---------------------------------------------------------- execution

    def run_now(self, task_id: int) -> None:
        """立即执行一次任务（无论是否启用）。"""
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"task #{task_id} 不存在")
        self._execute(dict(row))

    def _execute(self, task: dict) -> None:
        task_id = task["id"]
        task_type = task["task_type"]
        executor = self._executors.get(task_type)
        if executor is None:
            logger.warning("任务 #%s (%s) 类型 %r 未注册执行器，跳过", task_id, task["name"], task_type)
        else:
            try:
                executor(self, task)
            except Exception:
                logger.exception("任务 #%s (%s) 执行失败", task_id, task["name"])
        now = _now()
        next_run = self._next_run_after(task["cron_expr"], now)
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE scheduled_tasks SET last_run_at = ?, next_run_at = ? WHERE id = ?",
                (_fmt(now), _fmt(next_run) if next_run else None, task_id),
            )

    # ---------------------------------------------------------- executors

    @staticmethod
    def _exec_db_maintenance(scheduler: "Scheduler", task: dict) -> None:
        """WAL checkpoint + VACUUM。VACUUM 不能在事务内执行，需独立连接。"""
        conn = sqlite3.connect(scheduler.db_path, timeout=60)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            logger.info("db_maintenance 完成: %s", scheduler.db_path)
        finally:
            conn.close()

    @staticmethod
    def _exec_followup_reminder(scheduler: "Scheduler", task: dict) -> None:
        """统计 candidates 中超过 N 天未跟进的记录数并记日志。

        params: {"days": 3}。跟进时间列按常见命名自动探测。
        """
        try:
            params = json.loads(task.get("params_json") or "{}")
        except json.JSONDecodeError:
            params = {}
        days = int(params.get("days", 3))
        conn = sqlite3.connect(scheduler.db_path, timeout=30)
        try:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='candidates'"
            ).fetchone()
            if not table:
                logger.warning("followup_reminder: 库中无 candidates 表，跳过")
                return
            cols = {r[1] for r in conn.execute("PRAGMA table_info(candidates)")}
            follow_col = next(
                (c for c in ("last_followup_at", "last_follow_up_at", "followup_at", "updated_at") if c in cols),
                None,
            )
            if follow_col is None:
                logger.warning("followup_reminder: candidates 表无可用跟进时间列，跳过")
                return
            cutoff = _fmt(_now() - timedelta(days=days))
            count = conn.execute(
                f"SELECT COUNT(*) FROM candidates WHERE {follow_col} IS NOT NULL AND {follow_col} < ?",
                (cutoff,),
            ).fetchone()[0]
            logger.info("followup_reminder: 超过 %d 天未跟进的候选人 %d 条（列 %s）", days, count, follow_col)
        finally:
            conn.close()
