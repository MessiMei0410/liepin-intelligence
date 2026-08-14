from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from asa_core.app import create_app


SOURCE_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")


@pytest.fixture(scope="module")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # 模块级共享副本：整模块只复制一次生产库（1.5GB）。候选 559/560 的推荐确认
    # 幂等复用（already_confirmed 不重复生成）；metrics 测试先复位 559 阶段再操作。
    target = tmp_path_factory.mktemp("consultant-recommendation") / "asa.db"
    source = sqlite3.connect(SOURCE_DB)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def _reactivate_559(db_path: Path) -> None:
    """共享副本中前序测试可能把 559 置为停止：metrics 测试需要 559 可 preflight。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE job_candidates SET clean_stage='S1 新增寻访/待复核',flow_bucket='待复核',"
            "raw_status='search_shortlisted' WHERE id=559"
        )
        conn.commit()
    finally:
        conn.close()


def _preflight(client: TestClient, candidate_id: int, request_id: str = "cr-preflight-1") -> dict:
    response = client.post(
        "/api/v1/consultant-recommendations/preflight",
        json={"request_id": request_id, "candidate_id": candidate_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_consultant_recommendation_commit_requires_reason(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        preflight = _preflight(client, 559)
        missing = client.post(
            "/api/v1/consultant-recommendations/commit",
            headers={"Idempotency-Key": "cr-missing-reason"},
            json={
                "request_id": "cr-commit-missing",
                "candidate_id": 559,
                "preflight_token": preflight["token"],
            },
        )
        blank = client.post(
            "/api/v1/consultant-recommendations/commit",
            headers={"Idempotency-Key": "cr-blank-reason"},
            json={
                "request_id": "cr-commit-blank",
                "candidate_id": 559,
                "preflight_token": preflight["token"],
                "reason": "   ",
            },
        )
    assert missing.status_code == 422
    assert blank.status_code == 409
    assert "原因" in blank.json()["detail"]


def test_consultant_recommendation_commit_is_idempotent(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        body = {
            "request_id": "cr-commit-559",
            "candidate_id": 559,
            "preflight_token": _preflight(client, 559, "cr-preflight-559")["token"],
            "reason": "技术市场线轨迹清晰，已向客户提交嘉驰推荐报告",
        }
        first = client.post(
            "/api/v1/consultant-recommendations/commit",
            headers={"Idempotency-Key": "cr-key-559"},
            json=body,
        )
        replay = client.post(
            "/api/v1/consultant-recommendations/commit",
            headers={"Idempotency-Key": "cr-key-559"},
            json=body,
        )
        second = client.post(
            "/api/v1/consultant-recommendations/commit",
            headers={"Idempotency-Key": "cr-key-559-again"},
            json={
                "request_id": "cr-commit-559-again",
                "candidate_id": 559,
                "preflight_token": _preflight(client, 559, "cr-preflight-559-again")["token"],
                "reason": "客户渠道已读回，二次确认",
            },
        )

        conn = sqlite3.connect(db_path)
        try:
            fact_count = conn.execute(
                "SELECT COUNT(*) FROM consultant_confirmed_recommendations WHERE job_candidate_id=559"
            ).fetchone()[0]
            event_count = conn.execute(
                """SELECT COUNT(*) FROM candidate_events
                   WHERE job_candidate_id=559 AND event_type='consultant_confirmed_recommendation'"""
            ).fetchone()[0]
        finally:
            conn.close()

    assert first.status_code == 200
    assert first.json()["already_confirmed"] is False
    assert replay.status_code == 200
    assert replay.json()["receipt"]["idempotent_replay"] is True
    assert second.status_code == 200
    assert second.json()["already_confirmed"] is True
    assert fact_count == 1
    assert event_count == 1


def test_consultant_recommendation_rejects_stopped_candidate(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        already_stopped = client.post(
            "/api/v1/consultant-recommendations/preflight",
            json={"request_id": "cr-preflight-stopped", "candidate_id": 562},
        )

        preflight = _preflight(client, 559, "cr-preflight-stop-after")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE job_candidates SET clean_stage='H5 最近寻访/初筛不通过',raw_status='screen_rejected' WHERE id=559"
            )
            conn.commit()
        finally:
            conn.close()
        stopped_commit = client.post(
            "/api/v1/consultant-recommendations/commit",
            headers={"Idempotency-Key": "cr-key-stopped"},
            json={
                "request_id": "cr-commit-stopped",
                "candidate_id": 559,
                "preflight_token": preflight["token"],
                "reason": "想确认但关系已停止",
            },
        )
    assert already_stopped.status_code == 409
    assert "停止" in already_stopped.json()["detail"]
    assert stopped_commit.status_code == 409
    assert "停止" in stopped_commit.json()["detail"]


def test_consultant_recommendation_metrics(db_path: Path) -> None:
    _reactivate_559(db_path)  # 前序测试把 559 置为停止，这里复位后再 preflight/commit
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        for index, candidate_id in enumerate((559, 560), start=1):
            token = _preflight(client, candidate_id, f"cr-preflight-metric-{index}")["token"]
            commit = client.post(
                "/api/v1/consultant-recommendations/commit",
                headers={"Idempotency-Key": f"cr-key-metric-{candidate_id}"},
                json={
                    "request_id": f"cr-commit-metric-{index}",
                    "candidate_id": candidate_id,
                    "preflight_token": token,
                    "reason": f"候选人 {candidate_id} 已向客户推荐",
                },
            )
            assert commit.status_code == 200, commit.text

        metrics = client.get("/api/v1/jobs/154/recommendation-metrics")
        unknown = client.get("/api/v1/jobs/999999/recommendation-metrics")

        conn = sqlite3.connect(db_path)
        try:
            expected_assessed = conn.execute(
                """SELECT COUNT(DISTINCT a.job_candidate_id)
                     FROM agent_candidate_assessments a
                     JOIN agent_runs r ON r.run_id=a.run_id
                     JOIN job_candidates jc ON jc.id=a.job_candidate_id
                    WHERE a.is_current=1 AND r.status='completed' AND jc.job_id=154"""
            ).fetchone()[0]
            expected_confirmed = conn.execute(
                "SELECT COUNT(*) FROM consultant_confirmed_recommendations WHERE job_id=154"
            ).fetchone()[0]
        finally:
            conn.close()

    assert metrics.status_code == 200
    payload = metrics.json()
    assert payload["job_id"] == 154
    assert payload["confirmed_recommendations"] == expected_confirmed == 2
    assert payload["assessed_candidates"] == expected_assessed
    assert payload["rate"] == round(expected_confirmed / expected_assessed, 4)
    assert unknown.status_code == 404
