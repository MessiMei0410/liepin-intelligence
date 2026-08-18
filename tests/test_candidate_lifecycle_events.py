from __future__ import annotations

import sqlite3
from pathlib import Path
from _local import env_path, fixture_base_db, require_local

import pytest
from fastapi.testclient import TestClient

from asa_core.app import create_app


SOURCE_DB = env_path("ASA_SOURCE_DB", Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db"))


@pytest.fixture(scope="module")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # 模块级共享副本：候选人 559 事件均为追加写入且各测试用独立幂等键/request_id，
    # 断言均限定在本测试自己的记录上，共享安全。
    target = tmp_path_factory.mktemp("lifecycle-events") / "asa.db"
    require_local(SOURCE_DB, "正式库 talent_system_v3")
    source = sqlite3.connect(fixture_base_db())
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def _record(client: TestClient, candidate_id: int, key: str, request_id: str, **fields):
    return client.post(
        f"/api/v1/candidates/{candidate_id}/lifecycle-events",
        headers={"Idempotency-Key": key},
        json={"request_id": request_id, **fields},
    )


def _count(db: Path, sql: str, params: tuple = ()) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    finally:
        conn.close()


def test_lifecycle_event_writes_event_and_followup(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = _record(
            client, 559, "lc-key-1", "lc-req-1",
            event_type="interview_scheduled",
            occurred_at="2026-08-10 14:00",
            notes="一面：客户技术负责人",
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["already_recorded"] is False
    assert payload["event"]["event_type"] == "interview_scheduled"
    assert payload["event"]["event_type_label"] == "面试安排"
    assert payload["event"]["event_status"] == "scheduled"
    assert payload["event"]["event_time"] == "2026-08-10 14:00:00"
    assert "一面：客户技术负责人" in payload["event"]["summary"]
    # 自动跟进待办：截止 = 事件发生时间 + 2 天（interview_scheduled 口径）。
    assert payload["followup"]["task_type"] == "interview_followup"
    assert payload["followup"]["status"] == "open"
    assert payload["followup"]["due_at"] == "2026-08-12 14:00:00"
    assert _count(
        db_path,
        "SELECT COUNT(*) FROM candidate_events WHERE id=? AND event_type='interview_scheduled' AND source_table='api_v1' AND source_id='lc-req-1'",
        (payload["event_id"],),
    ) == 1
    assert _count(
        db_path,
        "SELECT COUNT(*) FROM followup_tasks WHERE id=? AND job_candidate_id=559 AND task_type='interview_followup' AND status='open' AND source_table='lifecycle_event'",
        (payload["followup_task_id"],),
    ) == 1


def test_lifecycle_event_default_time_and_status(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = _record(client, 559, "lc-key-default", "lc-req-default", event_type="onboarded")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["event"]["event_status"] == "recorded"
    assert payload["event"]["summary"] == "确认入职"
    assert payload["event"]["event_time"]
    assert payload["followup"]["task_type"] == "onboarding_followup"


def test_lifecycle_event_is_idempotent(db_path: Path) -> None:
    body = {"event_type": "offer_extended", "occurred_at": "2026-08-01 10:30", "notes": "已电话沟通薪资"}
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        first = _record(client, 559, "lc-key-idem", "lc-req-idem", **body)
        replay = _record(client, 559, "lc-key-idem", "lc-req-idem", **body)
        # 换一个 Idempotency-Key 但沿用同一 request_id：表级 source_id 去重兜底。
        dedup = _record(client, 559, "lc-key-idem-2", "lc-req-idem", **body)
    assert first.status_code == 200, first.text
    assert first.json()["already_recorded"] is False
    assert replay.status_code == 200
    assert replay.json()["receipt"]["idempotent_replay"] is True
    assert dedup.status_code == 200, dedup.text
    assert dedup.json()["already_recorded"] is True
    assert dedup.json()["event_id"] == first.json()["event_id"]
    assert _count(
        db_path,
        "SELECT COUNT(*) FROM candidate_events WHERE job_candidate_id=559 AND event_type='offer_extended' AND source_id='lc-req-idem'",
    ) == 1
    assert _count(
        db_path,
        "SELECT COUNT(*) FROM followup_tasks WHERE job_candidate_id=559 AND source_table='lifecycle_event' AND task_type='offer_followup'",
    ) == 1


def test_lifecycle_events_unified_in_timelines(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        job_id = int(conn.execute("SELECT job_id FROM job_candidates WHERE id=559").fetchone()[0])
        person_id = int(conn.execute("SELECT person_id FROM job_candidates WHERE id=559").fetchone()[0])
        # 旧口径 client_feedback 事件（event_status 承载反馈类型）保留可读。
        conn.execute(
            """INSERT INTO candidate_events(job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table)
               VALUES (559,?,?,'client_feedback','interviewing',datetime('now','localtime'),'客户反馈：进入面试','{}','legacy')""",
            (person_id, job_id),
        )
        conn.commit()
    finally:
        conn.close()
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        created = _record(client, 559, "lc-key-timeline", "lc-req-timeline", event_type="offer_accepted", notes="候选人已签回 Offer")
        assert created.status_code == 200, created.text
        detail = client.get("/api/v1/candidates/559")
        job = client.get(f"/api/v1/jobs/{job_id}")
    assert detail.status_code == 200, detail.text
    event_types = {event["event_type"] for event in detail.json()["candidate"]["events"]}
    assert "offer_accepted" in event_types
    assert "client_feedback" in event_types  # 新旧事件统一返回
    assert job.status_code == 200, job.text
    job_event_types = {event["event_type"] for event in job.json()["job"]["events"]}
    assert "offer_accepted" in job_event_types
    assert "client_feedback" in job_event_types


def test_lifecycle_event_validation(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        unknown_type = _record(client, 559, "lc-key-bad-type", "lc-req-bad-type", event_type="promoted")
        bad_time = _record(client, 559, "lc-key-bad-time", "lc-req-bad-time", event_type="onboarded", occurred_at="下周三上午")
        bad_status = _record(client, 559, "lc-key-bad-status", "lc-req-bad-status", event_type="onboarded", event_status="passed")
        missing = _record(client, 999999, "lc-key-missing", "lc-req-missing", event_type="onboarded")
    assert unknown_type.status_code == 409
    assert "未知生命周期事件类型" in unknown_type.json()["detail"]
    assert bad_time.status_code == 409
    assert "时间格式非法" in bad_time.json()["detail"]
    assert bad_status.status_code == 409
    assert "事件状态非法" in bad_status.json()["detail"]
    assert missing.status_code == 404


def test_ensure_lifecycle_followup_cleanup_without_table() -> None:
    """合成 fixture 库没有 followup_tasks 表时，孤儿清理安全跳过（CI 事故回归）。"""
    from asa_core.database import ensure_lifecycle_followup_cleanup

    conn = sqlite3.connect(":memory:")
    try:
        ensure_lifecycle_followup_cleanup(conn)  # 不抛 OperationalError
    finally:
        conn.close()


def test_lifecycle_event_explicit_status_written(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = _record(
            client, 559, "lc-key-status", "lc-req-status",
            event_type="interview_completed", event_status="passed", notes="一面通过，等二面",
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["event"]["event_status"] == "passed"
    assert _count(
        db_path,
        "SELECT COUNT(*) FROM candidate_events WHERE source_table='api_v1' AND source_id='lc-req-status' AND event_status='passed'",
    ) == 1


def test_migration_14_dedupes_and_enforces_unique_request_id(db_path: Path) -> None:
    """migration 14：先去重（保留最早一条 + 清孤儿待办）再建部分唯一索引；重复执行幂等。"""
    from asa_core.database import migrate

    conn = sqlite3.connect(db_path)
    try:
        person_id, job_id = conn.execute(
            "SELECT person_id,job_id FROM job_candidates WHERE id=559"
        ).fetchone()
        # 无论本模块其他测试是否已触发过 create_app 的 migrate，都回到 14 未应用的状态重跑。
        conn.execute("DROP INDEX IF EXISTS idx_candidate_events_api_v1_request")
        conn.execute("DELETE FROM schema_migrations WHERE version=14")
        conn.execute("DELETE FROM candidate_events WHERE source_id='m14-dup-req'")
        for summary in ("最早一条", "重复一条"):
            conn.execute(
                """INSERT INTO candidate_events(job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
                   VALUES (559,?,?,'interview_completed','completed',datetime('now','localtime'),?,'{}','api_v1','m14-dup-req')""",
                (person_id, job_id, summary),
            )
        dup_id = int(conn.execute(
            "SELECT MAX(id) FROM candidate_events WHERE source_id='m14-dup-req'"
        ).fetchone()[0])
        keep_id = int(conn.execute(
            "SELECT MIN(id) FROM candidate_events WHERE source_id='m14-dup-req'"
        ).fetchone()[0])
        orphan_task_id = int(conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM followup_tasks").fetchone()[0])
        conn.execute(
            """INSERT INTO followup_tasks(id,job_candidate_id,task_type,status,source_table,source_id,created_at,updated_at)
               VALUES (?,559,'interview_followup','open','lifecycle_event',?,datetime('now','localtime'),datetime('now','localtime'))""",
            (orphan_task_id, dup_id),
        )
        conn.commit()
    finally:
        conn.close()

    result = migrate(db_path, backup=False)
    assert 14 in result["applied"]

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id,summary FROM candidate_events WHERE source_id='m14-dup-req'"
        ).fetchall()
        assert [int(row[0]) for row in rows] == [keep_id]  # 既有重复保留最早一条
        assert conn.execute(
            "SELECT COUNT(*) FROM followup_tasks WHERE id=?", (orphan_task_id,)
        ).fetchone()[0] == 0  # 被删事件的孤儿待办一并清理
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_candidate_events_api_v1_request'"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO candidate_events(job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
                   VALUES (559,?,?,'interview_completed','passed',datetime('now','localtime'),'再次重复','{}','api_v1','m14-dup-req')""",
                (person_id, job_id),
            )
        conn.rollback()
        # 索引只约束 api_v1 且 source_id 非空的行：其他写入（source_id NULL / 其他 source_table）不受影响。
        conn.execute(
            """INSERT INTO candidate_events(job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table)
               VALUES (559,?,?,'client_feedback','interviewing',datetime('now','localtime'),'旧口径事件','{}','legacy')""",
            (person_id, job_id),
        )
        conn.commit()
    finally:
        conn.close()

    again = migrate(db_path, backup=False)
    assert 14 not in again["applied"]  # 幂等：重复执行不再应用


def test_lifecycle_event_concurrent_same_request_id(db_path: Path) -> None:
    """多客户端并发同一 request_id：只落一条事件与一条跟进待办，后到者按重放回读。"""
    import concurrent.futures

    from asa_core.service import CoreService

    core = CoreService(db_path=db_path)

    def record() -> dict:
        return core.record_lifecycle_event(
            559, "interview_scheduled", notes="并发写入", request_id="lc-req-race",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: record(), range(2)))
    assert all(result["ok"] for result in results)
    assert sum(1 for result in results if result["already_recorded"]) == 1
    assert len({result["event_id"] for result in results}) == 1
    assert _count(
        db_path,
        "SELECT COUNT(*) FROM candidate_events WHERE source_table='api_v1' AND source_id='lc-req-race'",
    ) == 1
    assert _count(
        db_path,
        "SELECT COUNT(*) FROM followup_tasks WHERE job_candidate_id=559 AND source_table='lifecycle_event' AND reason LIKE '面试安排：并发写入%'",
    ) == 1
