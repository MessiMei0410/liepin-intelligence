from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from _local import env_path, fixture_base_db, require_local

import pytest
from fastapi.testclient import TestClient

from asa_core.app import create_app


SOURCE_DB = env_path("ASA_SOURCE_DB", Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db"))


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    target = tmp_path / "asa.db"
    require_local(SOURCE_DB, "正式库 talent_system_v3")
    source = sqlite3.connect(fixture_base_db())
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
        json={"request_id": f"up-preflight-{key_suffix}", "candidate_id": candidate_id},
    )
    assert preflight.status_code == 200, preflight.text
    commit = client.post(
        "/api/v1/consultant-recommendations/commit",
        headers={"Idempotency-Key": f"up-commit-{key_suffix}"},
        json={
            "request_id": f"up-commit-{key_suffix}",
            "candidate_id": candidate_id,
            "preflight_token": preflight.json()["token"],
            "reason": reason,
        },
    )
    assert commit.status_code == 200, commit.text
    return commit.json()


def _bump_assessment(db_path: Path, candidate_id: int, key_suffix: str, fit_score: int) -> int:
    """模拟评估更新：旧 is_current=1 → 0，插入新 run + 新评估（is_current=1）。返回新 assessment id。"""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT person_id,job_id FROM agent_candidate_assessments WHERE job_candidate_id=? AND is_current=1",
            (candidate_id,),
        ).fetchone()
        assert row, "fixture 候选人缺少当前有效评估"
        person_id, job_id = row
        conn.execute(
            "UPDATE agent_candidate_assessments SET is_current=0 WHERE job_candidate_id=? AND is_current=1",
            (candidate_id,),
        )
        conn.execute(
            """INSERT INTO agent_runs (run_id,kind,context_type,context_id,snapshot_hash,status)
               VALUES (?,?,?,?,?,'completed')""",
            (f"run-upgrade-{key_suffix}", "candidate_assessment", "job_candidate", candidate_id, f"snap-{key_suffix}"),
        )
        cursor = conn.execute(
            """INSERT INTO agent_candidate_assessments
               (run_id,job_candidate_id,person_id,job_id,snapshot_hash,assessment_version,
                fit_score,fit_level,recommendation,confidence,evidence_coverage,
                strengths_json,gaps_json,risks_json,verification_questions_json,is_current)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (
                f"run-upgrade-{key_suffix}",
                candidate_id,
                person_id,
                job_id,
                f"snap-{key_suffix}",
                "test-v2",
                fit_score,
                "B+",
                "recommend",
                0.8,
                0.9,
                json.dumps(["升版后新优势"]),
                json.dumps(["升版后新缺口"]),
                json.dumps(["升版后新风险"]),
                json.dumps(["升版后待核验问题"]),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _upgrade_preflight(client: TestClient, package_id: str, key_suffix: str):
    return client.post(
        f"/api/v1/recommendation-packages/{package_id}/upgrade/preflight",
        json={"request_id": f"upg-preflight-{key_suffix}", "package_id": package_id},
    )


def _upgrade_commit(client: TestClient, package_id: str, token: str, key_suffix: str):
    return client.post(
        f"/api/v1/recommendation-packages/{package_id}/upgrade/commit",
        headers={"Idempotency-Key": f"upg-commit-{key_suffix}"},
        json={
            "request_id": f"upg-commit-{key_suffix}",
            "package_id": package_id,
            "preflight_token": token,
        },
    )


def test_upgrade_flow_generates_v2_and_keeps_v1(db_path: Path) -> None:
    """要点 1：确认推荐 → v1；评估更新 → upgradeable=true → 升版 → v2 存在、v1 保留。"""
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        package_v1 = _confirm(client, 559, "up1")["package"]
        assert package_v1["version"] == 1

        before = client.get(f"/api/v1/recommendation-packages/{package_v1['package_id']}")
        assert before.status_code == 200
        assert before.json()["upgradeable"] is False

        new_assessment_id = _bump_assessment(db_path, 559, "up1", 88)

        detail = client.get(f"/api/v1/recommendation-packages/{package_v1['package_id']}")
        assert detail.json()["upgradeable"] is True
        assert detail.json()["latest_assessment_id"] == new_assessment_id

        preflight = _upgrade_preflight(client, package_v1["package_id"], "up1")
        assert preflight.status_code == 200, preflight.text
        assert preflight.json()["current_version"] == 1
        assert preflight.json()["latest_assessment_id"] == new_assessment_id
        assert preflight.json()["latest_fingerprint"].startswith("sha256:")

        commit = _upgrade_commit(client, package_v1["package_id"], preflight.json()["token"], "up1")
        assert commit.status_code == 200, commit.text
        payload = commit.json()
        assert payload["upgraded"] is True
        assert payload["previous_version"] == 1
        package_v2 = payload["package"]
        assert package_v2["version"] == 2
        assert package_v2["status"] == "generated"
        assert package_v2["package_id"] != package_v1["package_id"]

        listing = client.get("/api/v1/candidates/559/recommendation-packages")
        v2_detail = client.get(f"/api/v1/recommendation-packages/{package_v2['package_id']}")

    items = listing.json()["items"]
    assert [item["version"] for item in items] == [2, 1]
    # v2：summary 继承 v1（人岗/推荐事实不变），证据快照换新评估。
    assert v2_detail.json()["summary"] == detail.json()["summary"]
    assert v2_detail.json()["evidence"]["assessment_id"] == new_assessment_id
    assert v2_detail.json()["evidence"]["fit_score"] == 88
    assert v2_detail.json()["evidence"]["fingerprint"] == preflight.json()["latest_fingerprint"]
    assert v2_detail.json()["upgradeable"] is False


def test_upgrade_preflight_409_without_new_assessment(db_path: Path) -> None:
    """要点 2：无更新评估 → preflight 409。"""
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        package_id = _confirm(client, 559, "up2")["package"]["package_id"]

        preflight = _upgrade_preflight(client, package_id, "up2")
        unknown = _upgrade_preflight(client, "recpkg_missing", "up2")

    assert preflight.status_code == 409
    assert "无需升版" in preflight.json()["detail"]
    assert unknown.status_code == 404


def test_upgrade_commit_idempotent_replay(db_path: Path) -> None:
    """要点 3：同 Idempotency-Key 重放 → 同 package_id，不重复生成。"""
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        package_id = _confirm(client, 559, "up3")["package"]["package_id"]
        _bump_assessment(db_path, 559, "up3", 77)
        token = _upgrade_preflight(client, package_id, "up3").json()["token"]

        first = _upgrade_commit(client, package_id, token, "up3")
        replay = _upgrade_commit(client, package_id, token, "up3")

        conn = sqlite3.connect(db_path)
        try:
            versions = conn.execute(
                "SELECT version FROM recommendation_packages WHERE job_candidate_id=559 ORDER BY version"
            ).fetchall()
        finally:
            conn.close()

    assert first.status_code == 200, first.text
    assert replay.status_code == 200
    assert replay.json()["receipt"]["idempotent_replay"] is True
    assert replay.json()["package"]["package_id"] == first.json()["package"]["package_id"]
    assert [row[0] for row in versions] == [1, 2]


def test_historical_version_readable_and_readonly(db_path: Path) -> None:
    """要点 4：历史版本详情可读、不可升版（只允许对最新版本升版）。"""
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        package_v1 = _confirm(client, 559, "up4")["package"]["package_id"]
        _bump_assessment(db_path, 559, "up4", 66)
        token = _upgrade_preflight(client, package_v1, "up4").json()["token"]
        assert _upgrade_commit(client, package_v1, token, "up4").status_code == 200

        detail = client.get(f"/api/v1/recommendation-packages/{package_v1}")
        preflight = _upgrade_preflight(client, package_v1, "up4-again")

    assert detail.status_code == 200
    assert detail.json()["version"] == 1
    assert detail.json()["upgradeable"] is False
    assert preflight.status_code == 409
    assert "只读" in preflight.json()["detail"]


def test_upgrade_commit_409_when_assessment_unchanged(db_path: Path) -> None:
    """要点 5：preflight 后评估回退（commit 时刻指纹与快照一致）→ 409。"""
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        package_id = _confirm(client, 559, "up5")["package"]["package_id"]
        _bump_assessment(db_path, 559, "up5", 55)
        token = _upgrade_preflight(client, package_id, "up5").json()["token"]

        # 预检与提交之间评估回退为旧评估（指纹回到包内快照）。
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE agent_candidate_assessments SET is_current=0 WHERE run_id='run-upgrade-up5'"
            )
            conn.execute(
                """UPDATE agent_candidate_assessments SET is_current=1
                   WHERE job_candidate_id=559 AND id=(
                       SELECT MAX(id) FROM agent_candidate_assessments
                       WHERE job_candidate_id=559 AND run_id != 'run-upgrade-up5')"""
            )
            conn.commit()
            package_count = conn.execute(
                "SELECT COUNT(*) FROM recommendation_packages WHERE job_candidate_id=559"
            ).fetchone()[0]
        finally:
            conn.close()

        commit = _upgrade_commit(client, package_id, token, "up5")

    assert commit.status_code == 409
    assert "无需升版" in commit.json()["detail"]
    assert package_count == 1
