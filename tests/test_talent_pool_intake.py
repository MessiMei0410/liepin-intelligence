"""pool-only（人才库储备）入库链路回归测试。

覆盖 2026-07-22 产品裁决：候选人入库不再强制 candidate+client+job 三者齐全，
缺客户/岗位或显式 pool_only 时先入库为"人才库储备"（不挂岗位）。
被测：/Users/messi/Documents/Codex/2026-06-26/re/work/talent_system_sync.py 的
process_action intake 分支。全程使用临时 DB 副本，绝不写正式库。
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

SYNC_SCRIPT = Path("/Users/messi/Documents/Codex/2026-06-26/re/work/talent_system_sync.py")
SOURCE_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")

spec = importlib.util.spec_from_file_location("talent_system_sync", SYNC_SCRIPT)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    target = tmp_path / "asa.db"
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


def _pool_action(**overrides: object) -> dict:
    action = {
        "kind": "candidate_intake",
        "action_id": "pool-test-001",
        "source_id": "pool-test:pool-test-001",
        "candidate": "测试储备人选",
        "company": "示例公司",
        "title": "示例职位",
        "client": "",
        "job": "",
        "source": "liepin",
    }
    action.update(overrides)
    return action


def _process(conn: sqlite3.Connection, action: dict, *, dry_run: bool = False, index: int = 0) -> dict:
    return sync.process_action(
        conn,
        action,
        batch_source="pool_test",
        default_event_time="",
        default_source_thread_id="",
        index=index,
        dry_run=dry_run,
    )


def test_pool_only_intake_writes_pool_records(db_path: Path) -> None:
    conn = _connect(db_path)
    try:
        jobs_before = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        job_candidates_before = conn.execute("SELECT COUNT(*) FROM job_candidates").fetchone()[0]
        result = _process(conn, _pool_action())
        conn.commit()

        assert result["status"] == "written"
        assert result["resolve_status"] == "pool_intake"
        assert "人才库储备" in result["reason"]
        assert result["job_candidate_id"] is None

        candidate_row = conn.execute(
            "SELECT * FROM candidates WHERE name = ?", ("测试储备人选",)
        ).fetchone()
        assert candidate_row is not None
        assert candidate_row["talent_pool"] == "猎聘/待分配"
        assert not (candidate_row["client"] or "").strip()
        assert not (candidate_row["position"] or "").strip()
        assert candidate_row["status"] == "pool"

        person_id = int(result["person_id"])
        profile = conn.execute(
            "SELECT * FROM source_profiles WHERE person_id = ? AND source_candidate_id = ?",
            (person_id, "liepin_plugin"),
        ).fetchone()
        assert profile is not None
        assert profile["source_type"] == "liepin"

        event = conn.execute(
            "SELECT * FROM candidate_events WHERE source_table = 'cross_thread_sync' AND source_id = ?",
            ("pool-test:pool-test-001",),
        ).fetchone()
        assert event is not None
        assert event["job_candidate_id"] is None
        assert event["job_id"] is None
        assert event["person_id"] == person_id
        assert event["event_type"] == "candidate_intake"
        assert event["event_status"] == "pool_intake"

        # 绝不写 job_candidates、绝不新建岗位
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == jobs_before
        assert conn.execute("SELECT COUNT(*) FROM job_candidates").fetchone()[0] == job_candidates_before
    finally:
        conn.close()


def test_pool_only_intake_repeat_is_already_exists(db_path: Path) -> None:
    conn = _connect(db_path)
    try:
        first = _process(conn, _pool_action())
        conn.commit()
        assert first["status"] == "written"
        second = _process(conn, _pool_action())
        conn.commit()
        assert second["status"] == "already_exists"
        count = conn.execute(
            "SELECT COUNT(*) FROM candidates WHERE name = ?", ("测试储备人选",)
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_explicit_pool_only_flag_bypasses_job_even_with_client_and_job(db_path: Path) -> None:
    conn = _connect(db_path)
    try:
        job_candidates_before = conn.execute("SELECT COUNT(*) FROM job_candidates").fetchone()[0]
        action = _pool_action(
            action_id="pool-test-explicit",
            source_id="pool-test:pool-test-explicit",
            client="长越科技",
            job="自动化软件高级工程师",
            pool_only=True,
        )
        result = _process(conn, action)
        conn.commit()
        assert result["status"] == "written"
        assert result["resolve_status"] == "pool_intake"
        assert result["job_candidate_id"] is None
        assert conn.execute("SELECT COUNT(*) FROM job_candidates").fetchone()[0] == job_candidates_before
    finally:
        conn.close()


def test_full_intake_with_client_and_job_unchanged(db_path: Path) -> None:
    conn = _connect(db_path)
    try:
        action = _pool_action(
            action_id="pool-test-full",
            source_id="pool-test:pool-test-full",
            client="长越科技",
            job="自动化软件高级工程师",
        )
        result = _process(conn, action)
        conn.commit()
        assert result["status"] == "written"
        assert result["resolve_status"] == "created_or_reused"
        assert result["job_candidate_id"]
        event = conn.execute(
            "SELECT * FROM candidate_events WHERE source_table = 'cross_thread_sync' AND source_id = ?",
            ("pool-test:pool-test-full",),
        ).fetchone()
        assert event is not None
        assert event["job_candidate_id"] is not None
        assert event["job_id"] is not None
        relation = conn.execute(
            "SELECT * FROM job_candidates WHERE id = ?", (int(result["job_candidate_id"]),)
        ).fetchone()
        assert relation is not None
    finally:
        conn.close()


def test_intake_without_candidate_still_pending_review(db_path: Path) -> None:
    conn = _connect(db_path)
    try:
        result = _process(
            conn,
            _pool_action(action_id="pool-test-no-cand", source_id="pool-test:pool-test-no-cand", candidate=""),
        )
        assert result["status"] == "pending_review"
        assert "candidate" in result["reason"]
    finally:
        conn.close()


def test_pool_only_dry_run_rolls_back_cleanly(db_path: Path) -> None:
    conn = _connect(db_path)
    try:
        result = _process(conn, _pool_action(), dry_run=True)
        assert result["status"] == "would_write"
        assert result["resolve_status"] == "pool_intake"
        conn.rollback()
        assert conn.execute(
            "SELECT COUNT(*) FROM candidates WHERE name = ?", ("测试储备人选",)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM candidate_events WHERE source_id = ?", ("pool-test:pool-test-001",)
        ).fetchone()[0] == 0
    finally:
        conn.close()
