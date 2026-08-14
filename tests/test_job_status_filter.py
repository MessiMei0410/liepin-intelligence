"""岗位状态过滤（2026-07-22 产品裁决）回归测试。

覆盖：
- 黑名单关键词匹配边界（a_system_agent/job_status.py 与 talent_system_sync.py 内置副本一致性）
- intake 完整三要素但岗位命中黑名单状态 → 自动降级 pool_intake
- _mentioned_jobs_for_copilot 不推荐/不定位黑名单状态岗位
全程临时 DB 副本，不碰正式库。
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from a_system_agent.job_status import (
    JOB_INTAKE_BLOCKED_KEYWORDS,
    job_status_intake_allowed,
    job_status_intake_blocked,
)
from a_system_agent.service import AgentService

SYNC_SCRIPT = Path("/Users/messi/Documents/Codex/2026-06-26/re/work/talent_system_sync.py")
SOURCE_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")

spec = importlib.util.spec_from_file_location("talent_system_sync", SYNC_SCRIPT)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


BLOCKED_SAMPLES = ["待启动", "P0紧急/待启动", "暂停", "已暂停", "关闭", "closed", "只读快照", "已拆分-保留历史", "误归属-已迁移", "归档"]
ALLOWED_SAMPLES = ["已发布", "已发布/推进中", "已搜索/可筛人", "谈薪中", "有反馈/待复盘", "有搜索计划", "未来新状态", "", None]


def test_blocked_keywords_hit_expected_statuses() -> None:
    for status in BLOCKED_SAMPLES:
        assert job_status_intake_blocked(status), status
        assert not job_status_intake_allowed(status), status
    for status in ALLOWED_SAMPLES:
        assert job_status_intake_allowed(status), status


def test_sync_embedded_keyword_list_matches_canonical() -> None:
    # 防止两处名单漂移
    assert tuple(sync.JOB_INTAKE_BLOCKED_KEYWORDS) == tuple(JOB_INTAKE_BLOCKED_KEYWORDS)
    for status in BLOCKED_SAMPLES + ALLOWED_SAMPLES:
        assert sync.job_status_intake_blocked(status) == job_status_intake_blocked(status), status


@pytest.fixture(scope="module")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # 模块级共享副本：整模块只复制一次生产库（1.5GB）。各测试用独立
    # action_id/source_id 入库（process_action 按 source_id 判重），互不冲突。
    target = tmp_path_factory.mktemp("job-status-filter") / "asa.db"
    source = sqlite3.connect(SOURCE_DB)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _insert_job(conn: sqlite3.Connection, client: str, title: str, status: str) -> int:
    conn.execute("INSERT OR IGNORE INTO clients (name) VALUES (?)", (client,))
    client_id = int(conn.execute("SELECT id FROM clients WHERE name = ?", (client,)).fetchone()["id"])
    # 共享副本：jobs 有 UNIQUE(client_id,title)；同名岗位已存在时复用（刷新状态），
    # 避免前序测试提交的同名岗位触发唯一约束冲突。
    existing = conn.execute(
        "SELECT id FROM jobs WHERE client_id=? AND title=?", (client_id, title)
    ).fetchone()
    if existing is not None:
        conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, existing["id"]))
        return int(existing["id"])
    job_id = int(conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM jobs").fetchone()[0])
    conn.execute(
        """
        INSERT INTO jobs (id, client_id, title, status, source_layer, updated_at)
        VALUES (?, ?, ?, ?, 'status_filter_test', datetime('now','localtime'))
        """,
        (job_id, client_id, title, status),
    )
    return job_id


def _intake_action(client: str, job: str, **overrides: object) -> dict:
    action = {
        "kind": "candidate_intake",
        "action_id": "status-filter-001",
        "source_id": "status-filter:status-filter-001",
        "candidate": "状态过滤测试人选",
        "company": "示例公司",
        "title": "示例职位",
        "client": client,
        "job": job,
        "source": "liepin",
    }
    action.update(overrides)
    return action


def _process(conn: sqlite3.Connection, action: dict) -> dict:
    return sync.process_action(
        conn,
        action,
        batch_source="status_filter_test",
        default_event_time="",
        default_source_thread_id="",
        index=0,
        dry_run=False,
    )


def test_intake_downgrades_to_pool_when_job_status_blocked(db_path: Path) -> None:
    conn = _connect(db_path)
    try:
        _insert_job(conn, "状态过滤客户", "分析设备专家", "待启动")
        conn.commit()
        job_candidates_before = conn.execute("SELECT COUNT(*) FROM job_candidates").fetchone()[0]
        result = _process(conn, _intake_action("状态过滤客户", "分析设备专家"))
        conn.commit()
        assert result["status"] == "written"
        assert result["resolve_status"] == "pool_intake"
        assert result["job_candidate_id"] is None
        assert "分析设备专家" in result["reason"]
        assert "待启动" in result["reason"]
        assert "人才库储备" in result["reason"]
        assert conn.execute("SELECT COUNT(*) FROM job_candidates").fetchone()[0] == job_candidates_before
        pool_row = conn.execute(
            "SELECT * FROM candidates WHERE name = ?", ("状态过滤测试人选",)
        ).fetchone()
        assert pool_row is not None
        assert pool_row["talent_pool"] == "猎聘/待分配"
    finally:
        conn.close()


def test_intake_downgrade_reason_visible_in_dry_run(db_path: Path) -> None:
    conn = _connect(db_path)
    try:
        _insert_job(conn, "状态过滤客户", "分析设备专家", "待启动")
        conn.commit()
        result = sync.process_action(
            conn,
            _intake_action("状态过滤客户", "分析设备专家", action_id="status-filter-dry", source_id="status-filter:status-filter-dry"),
            batch_source="status_filter_test",
            default_event_time="",
            default_source_thread_id="",
            index=0,
            dry_run=True,
        )
        conn.rollback()
        assert result["status"] == "would_write"
        assert result["resolve_status"] == "pool_intake"
        assert "待启动" in result["reason"]
    finally:
        conn.close()


def test_intake_not_downgraded_when_job_status_active(db_path: Path) -> None:
    conn = _connect(db_path)
    try:
        _insert_job(conn, "状态过滤客户", "推进中岗位", "已发布/推进中")
        conn.commit()
        # 共享副本按 source_id 判重：用独立 source_id 避免命中前序测试已入库的 intake
        result = _process(
            conn,
            _intake_action(
                "状态过滤客户", "推进中岗位",
                action_id="status-filter-active", source_id="status-filter:status-filter-active",
            ),
        )
        conn.commit()
        assert result["status"] == "written"
        assert result["resolve_status"] == "created_or_reused"
        assert result["job_candidate_id"]
    finally:
        conn.close()


def test_explicit_pool_only_still_wins_regardless_of_job_status(db_path: Path) -> None:
    conn = _connect(db_path)
    try:
        _insert_job(conn, "状态过滤客户", "推进中岗位", "已发布/推进中")
        conn.commit()
        result = _process(
            conn,
            _intake_action(
                "状态过滤客户", "推进中岗位", pool_only=True,
                action_id="status-filter-pool", source_id="status-filter:status-filter-pool",
            ),
        )
        conn.commit()
        assert result["resolve_status"] == "pool_intake"
        assert result["job_candidate_id"] is None
    finally:
        conn.close()


@pytest.fixture()
def service(db_path: Path):
    svc = AgentService(db_path, max_workers=1)
    try:
        yield svc
    finally:
        svc.close()


def test_mentioned_jobs_for_copilot_excludes_blocked_status_jobs(db_path: Path, service: AgentService) -> None:
    conn = _connect(db_path)
    try:
        _insert_job(conn, "状态过滤客户", "过滤测试岗位甲", "待启动")
        _insert_job(conn, "状态过滤客户", "过滤测试岗位乙", "已发布/推进中")
        conn.commit()
    finally:
        conn.close()
    mentioned = service._mentioned_jobs_for_copilot("状态过滤客户 过滤测试岗位甲 过滤测试岗位乙 还有人选吗")
    titles = {str(item.get("job") or "") for item in mentioned}
    assert "过滤测试岗位乙" in titles
    assert "过滤测试岗位甲" not in titles
