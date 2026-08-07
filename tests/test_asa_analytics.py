from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from asa_core.app import create_app
from a_system_agent.copilot_tools import execute_search_candidates


SOURCE_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    target = tmp_path / "asa-analytics.db"
    source = sqlite3.connect(SOURCE_DB)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def post(client: TestClient, path: str, body: dict) -> object:
    request_id = f"analytics-test-{abs(hash((path, str(body))))}"
    return client.post(
        path,
        json={"request_id": request_id, **body},
        headers={"Idempotency-Key": request_id},
    )


def patch(client: TestClient, path: str, body: dict) -> object:
    request_id = f"analytics-test-{abs(hash((path, str(body))))}"
    return client.patch(
        path,
        json={"request_id": request_id, **body},
        headers={"Idempotency-Key": request_id},
    )


def test_analytics_lifecycle_is_deterministic_immutable_and_read_only(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        conn = sqlite3.connect(db_path)
        try:
            business_rows_before = conn.execute("SELECT COUNT(*) FROM job_candidates").fetchone()[0]
            # fixture 复制正式库，agent_analysis_runs 可能已有历史 run（正式 Core 产生的），
            # 只能断言本测试新增的增量，不能断言绝对行数。
            analysis_runs_before = conn.execute("SELECT COUNT(*) FROM agent_analysis_runs").fetchone()[0]
        finally:
            conn.close()

        created = post(client, "/api/v1/analytics/runs", {
            "catalog_id": "operations_overview",
            "question": "今天最需要关注什么？",
            "scope": {"days": 7},
        })
        assert created.status_code == 201, created.json()
        first = created.json()["result"]
        assert first["schema_version"] == "analysis_result_v1"
        assert first["catalog_id"] == "operations_overview"
        assert first["status"] == "completed"
        assert all(metric["definition_version"] for metric in first["metrics"])
        assert len(first["references"]) <= 10

        fetched = client.get(f"/api/v1/analytics/runs/{first['run_id']}")
        assert fetched.status_code == 200
        assert fetched.json()["result"] == first
        assert "export_path" not in fetched.json()

        exported = post(client, f"/api/v1/analytics/runs/{first['run_id']}/export", {})
        assert exported.status_code == 200, exported.json()
        artifact = exported.json()["artifact"]
        assert "file_path" not in artifact
        assert artifact["download_url"].endswith(f"/{first['run_id']}/download")
        downloaded = client.get(artifact["download_url"])
        assert downloaded.status_code == 200
        assert downloaded.headers["content-type"].startswith("text/markdown")
        assert first["headline"] in downloaded.text

        refreshed = post(client, f"/api/v1/analytics/runs/{first['run_id']}/refresh", {})
        assert refreshed.status_code == 201, refreshed.json()
        second = refreshed.json()["result"]
        assert second["run_id"] != first["run_id"]
        assert second["supersedes_run_id"] == first["run_id"]
        assert client.get(f"/api/v1/analytics/runs/{first['run_id']}").json()["result"] == first

        rejected = post(client, "/api/v1/analytics/runs", {
            "catalog_id": "operations_overview", "scope": {"raw_sql": "DELETE FROM jobs"},
        })
        assert rejected.status_code == 409
        assert "未授权字段" in rejected.json()["detail"]

        conn = sqlite3.connect(db_path)
        try:
            business_rows_after = conn.execute("SELECT COUNT(*) FROM job_candidates").fetchone()[0]
            stored = conn.execute("SELECT COUNT(*) FROM agent_analysis_runs").fetchone()[0]
        finally:
            conn.close()
        assert business_rows_after == business_rows_before
        assert stored - analysis_runs_before == 2


def test_expired_analysis_remains_readable_and_can_be_refreshed(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        created = post(client, "/api/v1/analytics/runs", {
            "catalog_id": "data_quality", "question": "数据质量如何？", "scope": {},
        }).json()["result"]
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE agent_analysis_runs SET expires_at='2020-01-01 00:00:00' WHERE run_id=?",
                (created["run_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        expired = client.get(f"/api/v1/analytics/runs/{created['run_id']}")
        assert expired.status_code == 200
        assert expired.json()["ok"] is False
        assert expired.json()["result"]["status"] == "expired"

        refreshed = post(client, f"/api/v1/analytics/runs/{created['run_id']}/refresh", {})
        assert refreshed.status_code == 201
        assert refreshed.json()["result"]["status"] == "completed"
        assert refreshed.json()["result"]["supersedes_run_id"] == created["run_id"]


def test_operations_and_job_health_use_canonical_active_job_scope(db_path: Path) -> None:
    active_job_sql = """COALESCE(lifecycle_stage,'') IN
        ('sourcing','published','active_pipeline','client_feedback','offer')
        AND COALESCE(status,'') NOT IN ('误归属-已迁移到视源电子','只读快照')"""
    conn = sqlite3.connect(db_path)
    try:
        expected_active_jobs = conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE {active_job_sql}"
        ).fetchone()[0]
        inactive_job_ids = {
            row[0] for row in conn.execute(
                f"SELECT id FROM jobs WHERE NOT ({active_job_sql})"
            ).fetchall()
        }
    finally:
        conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        overview = post(client, "/api/v1/analytics/runs", {
            "catalog_id": "operations_overview",
            "scope": {"days": 7},
        }).json()["result"]
        metrics = {metric["id"]: metric["value"] for metric in overview["metrics"]}
        assert metrics["active_jobs"] == expected_active_jobs
        assert not ({reference["id"] for reference in overview["references"]} & inactive_job_ids)

        health = post(client, "/api/v1/analytics/runs", {
            "catalog_id": "job_health",
            "scope": {"days": 30},
        }).json()["result"]
        assert not ({reference["id"] for reference in health["references"]} & inactive_job_ids)


def test_templates_workbench_and_legacy_candidate_search(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        template = post(client, "/api/v1/analytics/templates", {
            "name": "每日经营概览", "catalog_id": "operations_overview",
            "question": "今天先做什么？", "scope": {"days": 7},
        })
        assert template.status_code == 201, template.json()
        template_id = template.json()["template_id"]
        run = post(client, f"/api/v1/analytics/templates/{template_id}/run", {})
        assert run.status_code == 201, run.json()
        templates = client.get("/api/v1/analytics/templates").json()["items"]
        assert templates[0]["last_run_id"] == run.json()["result"]["run_id"]

        first = client.get("/api/v1/workbench?limit=300")
        second = client.get("/api/v1/workbench?limit=300")
        assert first.status_code == 200, first.json()
        assert first.json()["version"] == second.json()["version"]
        assert len({item["item_key"] for item in first.json()["items"]}) == len(first.json()["items"])
        assert any(item["kind"] == "analysis" for item in first.json()["items"])
        assert first.json()["summary"]["delivered"] >= 1
        assert first.json()["summary"]["total"] >= len(first.json()["items"])
        assert first.json()["returned_count"] == len(first.json()["items"])
        assert first.json()["truncated"] is (first.json()["summary"]["total"] > len(first.json()["items"]))

    conn = sqlite3.connect(db_path)
    try:
        name = conn.execute(
            """SELECT p.display_name FROM people p JOIN job_candidates jc ON jc.person_id=p.id
               WHERE trim(p.display_name)<>'' ORDER BY jc.id LIMIT 1"""
        ).fetchone()[0]
    finally:
        conn.close()
    result = execute_search_candidates(str(db_path), name=str(name), limit=2)
    assert result["success"] is True
    assert result["data"]["candidates"]


def test_scheduled_templates_can_be_managed_run_and_trended(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        created = post(client, "/api/v1/analytics/templates", {
            "name": "每日经营变化", "catalog_id": "operations_overview",
            "question": "经营指标发生了什么变化？", "scope": {"days": 7},
            "schedule_kind": "daily", "schedule_enabled": True,
            "schedule_time": "09:30", "timezone": "Asia/Shanghai",
        })
        assert created.status_code == 201, created.json()
        template_id = created.json()["template_id"]
        template = client.get("/api/v1/analytics/templates").json()["items"][0]
        assert template["schedule_enabled"] is True
        assert template["schedule_kind"] == "daily"
        assert template["next_run_at"]

        disabled = patch(client, f"/api/v1/analytics/templates/{template_id}", {"schedule_enabled": False})
        assert disabled.status_code == 200, disabled.json()
        template = client.get("/api/v1/analytics/templates").json()["items"][0]
        assert template["schedule_enabled"] is False
        assert template["next_run_at"] is None

        enabled = patch(client, f"/api/v1/analytics/templates/{template_id}", {
            "name": "每日经营变化追踪", "schedule_enabled": True,
        })
        assert enabled.status_code == 200, enabled.json()
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE agent_analysis_templates SET next_run_at='2020-01-01T00:00:00+00:00' WHERE template_id=?",
                (template_id,),
            )
            conn.commit()
        finally:
            conn.close()

        due = client.app.state.analytics.run_due_templates()
        assert due["claimed"] == 1
        assert due["results"][0]["template_run"]["trigger"] == "schedule"
        scheduled_run_id = due["results"][0]["result"]["run_id"]
        fetched_scheduled = client.get(f"/api/v1/analytics/runs/{scheduled_run_id}").json()
        assert fetched_scheduled["template_id"] == template_id
        first_result = fetched_scheduled["result"]

        manual = post(client, f"/api/v1/analytics/templates/{template_id}/run", {})
        assert manual.status_code == 201, manual.json()
        history = client.get(f"/api/v1/analytics/templates/{template_id}/runs").json()["items"]
        assert [item["trigger"] for item in history[:2]] == ["manual", "schedule"]
        assert all(item["status"] == "completed" for item in history[:2])

        trend = client.get(f"/api/v1/analytics/templates/{template_id}/trend").json()
        assert trend["run_count"] == 2
        assert trend["series"]
        assert all(len(series["points"]) == 2 for series in trend["series"])
        assert client.get(f"/api/v1/analytics/runs/{scheduled_run_id}").json()["result"] == first_result


def test_overlapping_scheduled_template_is_skipped_with_receipt(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        created = post(client, "/api/v1/analytics/templates", {
            "name": "重叠保护", "catalog_id": "data_quality", "scope": {},
            "schedule_kind": "daily", "schedule_enabled": True, "schedule_time": "10:00",
        })
        template_id = created.json()["template_id"]
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """INSERT INTO agent_analysis_template_runs
                   (template_run_id,template_id,trigger,status,started_at)
                   VALUES ('template_run_active',?,'manual','running',?)""",
                (template_id, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            conn.execute(
                """UPDATE agent_analysis_templates
                   SET next_run_at='2020-01-01T00:00:00+00:00',last_status='running' WHERE template_id=?""",
                (template_id,),
            )
            conn.commit()
        finally:
            conn.close()

        result = client.app.state.analytics.run_due_templates()
        assert result["claimed"] == 1
        assert result["results"][0]["skipped"] is True
        history = client.get(f"/api/v1/analytics/templates/{template_id}/runs").json()["items"]
        assert history[0]["status"] == "skipped"
        assert "正在运行" in history[0]["error"]


def test_copilot_analysis_card_is_persisted_and_restored(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = post(client, "/api/v1/copilot/messages", {
            "session_id": "analytics-copilot-session",
            "message": "今天最需要先做什么？",
            "context": {"type": "page", "page": "overview"},
        })
        assert response.status_code == 200, response.json()
        body = response.json()
        card = body["analysis_card"]
        assert card["schema_version"] == "analysis_card_v1"
        assert card["catalog_id"] == "operations_overview"
        assert card["open_analysis"] == {
            "type": "open_analysis", "id": card["run_id"], "label": "查看完整分析"
        }
        assert len(card["metrics"]) <= 5

        conn = sqlite3.connect(db_path)
        try:
            structured = conn.execute(
                """SELECT structured_json FROM agent_copilot_messages
                   WHERE session_id='analytics-copilot-session' AND role='assistant' ORDER BY id DESC LIMIT 1"""
            ).fetchone()[0]
        finally:
            conn.close()
        assert '"analysis_card"' in structured

        restored = client.app.state.core.agent_service.get_copilot_session("analytics-copilot-session")
        restored_card = restored["messages"][-1]["analysis_card"]
        assert restored_card["run_id"] == card["run_id"]
        assert restored["messages"][-1]["suggested_actions"] == [card["open_analysis"]]


def test_native_floating_surface_renders_and_opens_analysis_cards() -> None:
    source = (Path(__file__).parents[1] / "scripts" / "liepin_workbench_server.py").read_text(encoding="utf-8")
    for marker in (
        "renderAnalysisCard(message, index)", "message?.analysis_card", "data-analysis-open",
        "analysis_card: result.analysis_card || null", "runMessageAction('open_analysis', card.run_id)",
        "openWorkbenchUrl(`/asa-app#analysis=${encodeURIComponent(id)}`)",
    ):
        assert marker in source
