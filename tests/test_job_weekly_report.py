from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from asa_core.app import create_app


SOURCE_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")


@pytest.fixture(scope="module")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # 模块级共享副本：整模块只复制一次生产库（1.5GB）。周报 artifact 按周幂等
    # 合并（同周共用一个 artifact_id）；list 空态测试先清空本类型 artifact 再断言。
    target = tmp_path_factory.mktemp("job-weekly-report") / "asa.db"
    source = sqlite3.connect(SOURCE_DB)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def _clear_weekly_reports(db_path: Path) -> None:
    """共享副本：'空列表→生成→恰好 1 条' 断言需要清空本模块已生成的周报 artifact。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM agent_artifacts WHERE artifact_type='job_weekly_report'")
        conn.commit()
    finally:
        conn.close()


def _generate(client: TestClient, job_id: int, key: str, request_id: str) -> dict:
    response = client.post(
        f"/api/v1/jobs/{job_id}/weekly-report",
        headers={"Idempotency-Key": key},
        json={"request_id": request_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_job_weekly_report_generate_is_idempotent_and_versioned(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        first = _generate(client, 154, "jwr-key-1", "jwr-req-1")
        replay = client.post(
            "/api/v1/jobs/154/weekly-report",
            headers={"Idempotency-Key": "jwr-key-1"},
            json={"request_id": "jwr-req-1"},
        )
        second = _generate(client, 154, "jwr-key-2", "jwr-req-2")

        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT artifact_id,title,validation_status FROM agent_artifacts WHERE artifact_type='job_weekly_report'"
            ).fetchall()
        finally:
            conn.close()

    assert first["artifact_id"] == f"job_weekly_154_{first['week_start']}"
    assert first["version"] == 1
    assert replay.status_code == 200
    assert replay.json()["receipt"]["idempotent_replay"] is True
    # 同周新请求幂等更新同一 artifact，version 自增
    assert second["artifact_id"] == first["artifact_id"]
    assert second["version"] == 2
    assert len(rows) == 1
    assert rows[0][1].endswith("v2")
    assert rows[0][2] == "passed"


def test_job_weekly_report_unknown_job(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        generate = client.post(
            "/api/v1/jobs/999999/weekly-report",
            headers={"Idempotency-Key": "jwr-key-404"},
            json={"request_id": "jwr-req-404"},
        )
        listing = client.get("/api/v1/jobs/999999/weekly-reports")
    assert generate.status_code == 404
    assert listing.status_code == 404


def test_job_weekly_report_list_empty_then_latest(db_path: Path) -> None:
    _clear_weekly_reports(db_path)  # 空列表断言需要清空共享副本中前序测试生成的周报
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        empty = client.get("/api/v1/jobs/154/weekly-reports")
        assert empty.status_code == 200
        assert empty.json()["latest"] is None
        assert empty.json()["items"] == []

        created = _generate(client, 154, "jwr-key-list", "jwr-req-list")
        listing = client.get("/api/v1/jobs/154/weekly-reports")

    payload = listing.json()
    assert payload["job_id"] == 154
    assert len(payload["items"]) == 1
    latest = payload["latest"]
    assert latest["artifact_id"] == created["artifact_id"]
    assert latest["week_start"] == created["week_start"]
    assert latest["summary"]["total"] is not None
    assert latest["summary"]["risk_count"] >= 0


def test_job_weekly_report_content_sections(db_path: Path) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        # app 启动即完成迁移（recommendation_package_feedback 等表就绪），再埋本周数据
        conn = sqlite3.connect(db_path)
        try:
            # 本周推荐包反馈（approved ×1 / rejected ×1）
            for index, feedback_type in enumerate(("approved", "rejected"), start=1):
                conn.execute(
                    """INSERT INTO recommendation_package_feedback
                       (package_id,package_version,job_candidate_id,person_id,job_id,feedback_type,content,feedback_time,recorded_by,request_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (f"pkg_test_{index}", 1, 559, 1, 154, feedback_type, "测试反馈", now, "consultant", f"jwr-fb-{index}"),
                )
            # 本周触达 6 人次 0 回复 → 触发「触达无回复」风险与建议
            for index in range(6):
                conn.execute(
                    """INSERT INTO candidate_events
                       (job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary)
                       VALUES (?,?,?,?,?,?,?)""",
                    (559, 1, 154, "liepin_outreach", "done", now, f"触达{index}"),
                )
            conn.commit()
        finally:
            conn.close()

        created = _generate(client, 154, "jwr-key-content", "jwr-req-content")
        artifact = client.get(f"/api/v1/artifacts/{created['artifact_id']}")

    assert artifact.status_code == 200
    detail = artifact.json()["artifact"]
    content = detail["content"]
    assert detail["mime_type"] == "text/markdown"
    for heading in ("一、漏斗概览", "二、有效推荐", "三、渠道质量", "四、风险", "五、建议", "口径说明"):
        assert heading in content
    # 首期周报：无上期对比
    assert "无上期对比" in content
    assert "客户认可 1" in content
    assert "客户否决 1" in content
    assert "触达无回复" in content
    assert "0 回复" in content

    metadata = detail["metadata"]
    assert metadata["schema_version"] == "job_weekly_report_v1"
    assert metadata["job_id"] == 154
    assert metadata["recommendations"]["feedback"]["approved"] == 1
    assert metadata["recommendations"]["feedback"]["rejected"] == 1
    assert metadata["outreach"]["outreach_count"] >= 6
    assert any(risk["code"] == "outreach_no_reply" for risk in metadata["risks"])
    assert metadata["funnel"]["comparison"] == "no_baseline"


def test_job_weekly_report_uses_previous_report_as_baseline(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        # 手工埋一期上周周报 artifact，作为本期对比基线
        last_week = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        baseline = {
            "schema_version": "job_weekly_report_v1",
            "job_id": 154,
            "week_start": last_week,
            "generated_at": f"{last_week} 09:00:00",
            "version": 1,
            "history": [],
            "funnel": {"current": {"total": 1, "active": 1, "contacted": 0, "recommended": 0, "stopped": 0}},
        }
        conn.execute(
            """INSERT INTO agent_artifacts
               (artifact_id,goal_id,workflow_id,step_id,artifact_type,title,mime_type,file_path,content,metadata_json,validation_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"job_weekly_154_{last_week}", "job_weekly_154", "job_weekly_154", None,
                "job_weekly_report", "岗位周报 基线", "text/markdown", None, "基线",
                json.dumps(baseline, ensure_ascii=False), "passed",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        created = _generate(client, 154, "jwr-key-baseline", "jwr-req-baseline")
        artifact = client.get(f"/api/v1/artifacts/{created['artifact_id']}")
        listing = client.get("/api/v1/jobs/154/weekly-reports")

    metadata = artifact.json()["artifact"]["metadata"]
    assert metadata["funnel"]["comparison"] == "available"
    assert metadata["funnel"]["previous"]["total"] == 1
    assert "无上期对比" not in artifact.json()["artifact"]["content"]
    # 历史列表新→旧，含两期
    assert len(listing.json()["items"]) == 2
