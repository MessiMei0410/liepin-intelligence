from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from asa_core.app import create_app


SOURCE_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")


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


def _confirm(client: TestClient, candidate_id: int, key_suffix: str, reason: str = "技术市场线轨迹清晰，已向客户提交嘉驰推荐报告") -> dict:
    preflight = client.post(
        "/api/v1/consultant-recommendations/preflight",
        json={"request_id": f"rp-preflight-{key_suffix}", "candidate_id": candidate_id},
    )
    assert preflight.status_code == 200, preflight.text
    commit = client.post(
        "/api/v1/consultant-recommendations/commit",
        headers={"Idempotency-Key": f"rp-commit-{key_suffix}"},
        json={
            "request_id": f"rp-commit-{key_suffix}",
            "candidate_id": candidate_id,
            "preflight_token": preflight.json()["token"],
            "reason": reason,
        },
    )
    assert commit.status_code == 200, commit.text
    return commit.json()


def test_commit_generates_package_v1_and_is_idempotent(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        first = _confirm(client, 559, "559")
        second = _confirm(client, 559, "559-again", reason="二次确认不重复生成")

        detail = client.get("/api/v1/candidates/559")

        conn = sqlite3.connect(db_path)
        try:
            package_count = conn.execute(
                "SELECT COUNT(*) FROM recommendation_packages WHERE job_candidate_id=559"
            ).fetchone()[0]
        finally:
            conn.close()

    assert first["already_confirmed"] is False
    package = first["package"]
    assert package["version"] == 1
    assert package["status"] == "generated"
    assert package["package_id"].startswith("recpkg_")
    assert second["already_confirmed"] is True
    assert second["package"]["package_id"] == package["package_id"]
    assert package_count == 1
    assert detail.status_code == 200
    packages = detail.json()["candidate"]["recommendation_packages"]
    assert len(packages) == 1
    assert packages[0]["package_id"] == package["package_id"]
    assert packages[0]["feedback_count"] == 0


def test_package_list_and_detail(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        package_id = _confirm(client, 559, "list")["package"]["package_id"]

        listing = client.get("/api/v1/candidates/559/recommendation-packages")
        detail = client.get(f"/api/v1/recommendation-packages/{package_id}")
        unknown_package = client.get("/api/v1/recommendation-packages/recpkg_missing")
        unknown_candidate = client.get("/api/v1/candidates/999999/recommendation-packages")
        empty_candidate = client.get("/api/v1/candidates/560/recommendation-packages")

    assert listing.status_code == 200
    items = listing.json()["items"]
    assert [item["package_id"] for item in items] == [package_id]
    assert items[0]["version"] == 1

    assert detail.status_code == 200
    payload = detail.json()
    assert payload["candidate_id"] == 559
    assert payload["job_id"] == 154
    assert payload["summary"]["name"]
    assert payload["summary"]["recommendation"]["reason"]
    assert payload["evidence"]["status"] == "ready"
    assert payload["evidence"]["fit_score"] == 49
    assert payload["evidence"]["strengths"]
    assert payload["risks"]
    assert payload["verification_questions"]
    assert payload["feedback"] == []

    assert unknown_package.status_code == 404
    assert unknown_candidate.status_code == 404
    assert empty_candidate.status_code == 200
    assert empty_candidate.json()["items"] == []


def test_package_feedback_record_and_replay(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        package_id = _confirm(client, 559, "fb")["package"]["package_id"]

        body = {
            "request_id": "rp-feedback-1",
            "feedback_type": "interview",
            "content": "客户安排了首轮面试，反馈候选人表达清晰",
        }
        first = client.post(
            f"/api/v1/recommendation-packages/{package_id}/feedback",
            headers={"Idempotency-Key": "rp-feedback-key-1"},
            json=body,
        )
        replay = client.post(
            f"/api/v1/recommendation-packages/{package_id}/feedback",
            headers={"Idempotency-Key": "rp-feedback-key-1"},
            json=body,
        )
        detail = client.get(f"/api/v1/recommendation-packages/{package_id}")

        conn = sqlite3.connect(db_path)
        try:
            feedback_count = conn.execute(
                "SELECT COUNT(*) FROM recommendation_package_feedback WHERE package_id=?", (package_id,)
            ).fetchone()[0]
            event = conn.execute(
                """SELECT event_type,event_status,summary FROM candidate_events
                   WHERE job_candidate_id=559 AND event_type='client_feedback'"""
            ).fetchone()
        finally:
            conn.close()

    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["ok"] is True
    assert payload["already_recorded"] is False
    assert payload["package_version"] == 1
    assert payload["feedback"]["feedback_type"] == "interview"
    assert payload["feedback"]["feedback_type_label"] == "安排面试"
    assert payload["feedback"]["feedback_time"]
    assert payload["event_id"]

    assert replay.status_code == 200
    assert replay.json()["receipt"]["idempotent_replay"] is True
    assert feedback_count == 1

    assert event is not None
    assert event[1] == "interview"
    assert "推荐包 v1" in event[2]

    feedback_items = detail.json()["feedback"]
    assert len(feedback_items) == 1
    assert feedback_items[0]["feedback_type_label"] == "安排面试"
    assert feedback_items[0]["content"] == body["content"]


def test_package_feedback_validation(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        package_id = _confirm(client, 559, "validation")["package"]["package_id"]
        url = f"/api/v1/recommendation-packages/{package_id}/feedback"

        bad_type = client.post(
            url,
            headers={"Idempotency-Key": "rp-feedback-bad-type"},
            json={"request_id": "rp-feedback-bad-type", "feedback_type": "unknown", "content": "内容"},
        )
        blank_content = client.post(
            url,
            headers={"Idempotency-Key": "rp-feedback-blank"},
            json={"request_id": "rp-feedback-blank", "feedback_type": "approved", "content": "   "},
        )
        missing_content = client.post(
            url,
            headers={"Idempotency-Key": "rp-feedback-missing"},
            json={"request_id": "rp-feedback-missing", "feedback_type": "approved"},
        )
        unknown_package = client.post(
            "/api/v1/recommendation-packages/recpkg_missing/feedback",
            headers={"Idempotency-Key": "rp-feedback-404"},
            json={"request_id": "rp-feedback-404", "feedback_type": "approved", "content": "内容"},
        )

    assert bad_type.status_code == 409
    assert "反馈类型" in bad_type.json()["detail"]
    assert blank_content.status_code == 409
    assert "不能为空" in blank_content.json()["detail"]
    assert missing_content.status_code == 422
    assert unknown_package.status_code == 404
