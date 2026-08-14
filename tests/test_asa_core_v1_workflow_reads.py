from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from asa_core.app import create_app


SOURCE_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
BIG_STDOUT = "AUDIT-STDOUT-" + "x" * 300_000

# 与 WorkflowEngine.get_workflow_candidates 的统计口径保持一致
EXPECTED_TOTAL_SQL = """
SELECT COUNT(*)
FROM job_candidates jc
JOIN people p ON p.id=jc.person_id
LEFT JOIN agent_candidate_assessments a ON a.id=(
    SELECT a2.id FROM agent_candidate_assessments a2
    JOIN agent_runs r2 ON r2.run_id=a2.run_id
    WHERE a2.job_candidate_id=jc.id AND a2.is_current=1 AND r2.status='completed'
    ORDER BY a2.id DESC LIMIT 1
)
LEFT JOIN agent_sourcing_attributions sa ON sa.id=(
    SELECT sa2.id FROM agent_sourcing_attributions sa2
    WHERE sa2.job_candidate_id=jc.id ORDER BY sa2.id DESC LIMIT 1
)
WHERE jc.job_id=? AND (a.id IS NOT NULL OR sa.id IS NOT NULL)
"""


def _pick_fixtures(db: Path) -> dict:
    conn = sqlite3.connect(db)
    try:
        job_workflow = conn.execute(
            """
            SELECT w.workflow_id,
                   json_extract(g.context_json,'$.id') AS job_id,
                   (SELECT s.id FROM agent_workflow_steps s
                     WHERE s.workflow_id=w.workflow_id AND s.capability_id='candidate_batch_assessment'
                     ORDER BY s.sequence LIMIT 1) AS assessment_step_id,
                   (SELECT s.id FROM agent_workflow_steps s
                     WHERE s.workflow_id=w.workflow_id AND s.capability_id!='candidate_batch_assessment'
                     ORDER BY s.sequence LIMIT 1) AS audited_step_id
            FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
            WHERE json_extract(g.context_json,'$.type')='job'
              AND json_extract(g.context_json,'$.id') IS NOT NULL
              AND EXISTS (
                    SELECT 1 FROM agent_workflow_steps s
                    WHERE s.workflow_id=w.workflow_id AND s.capability_id='candidate_batch_assessment'
              )
              AND (
                    SELECT COUNT(*) FROM agent_candidate_assessments a
                    JOIN job_candidates jc ON jc.id=a.job_candidate_id
                    JOIN agent_runs r ON r.run_id=a.run_id
                    WHERE jc.job_id=json_extract(g.context_json,'$.id')
                      AND a.is_current=1 AND r.status='completed'
              ) > 0
            ORDER BY w.id LIMIT 1
            """
        ).fetchone()
        assert job_workflow, "fixture 需要一个带批量评估步骤且有已评估人选的岗位工作流"
        other_step = conn.execute(
            "SELECT id FROM agent_workflow_steps WHERE workflow_id!=? ORDER BY id LIMIT 1", (job_workflow[0],)
        ).fetchone()
        page_workflow = conn.execute(
            """
            SELECT w.workflow_id FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
            WHERE COALESCE(json_extract(g.context_json,'$.type'),'')!='job' ORDER BY w.id LIMIT 1
            """
        ).fetchone()
        assert other_step and page_workflow
        return {
            "workflow_id": job_workflow[0],
            "job_id": int(job_workflow[1]),
            "assessment_step_id": int(job_workflow[2]),
            "audited_step_id": int(job_workflow[3]),
            "other_step_id": int(other_step[0]),
            "page_workflow_id": page_workflow[0],
        }
    finally:
        conn.close()


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> dict:
    target = tmp_path_factory.mktemp("asa-workflow-reads") / "asa.db"
    source = sqlite3.connect(SOURCE_DB)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    meta = _pick_fixtures(target)
    conn = sqlite3.connect(target)
    try:
        output = {
            "summary": "多渠道寻访完成",
            "channel_runs": [{"channel": "liepin", "status": "completed"}],
            "audit": {"ok": True, "stdout": BIG_STDOUT},
            "strategy_v2": {
                "schema_version": "strategy_v2",
                "archetype_id": "power_rd_expert_computing",
                "input_level": "L2",
                "coverage_report": {"consumed_count": 3},
                "consultant_judgement": {
                    "version": "senior_consultant_v1",
                    "role_diagnosis": {"candidate_archetype": "项目证据优先的电源研发专家"},
                },
            },
        }
        conn.execute(
            "UPDATE agent_workflow_steps SET output_json=? WHERE id=?",
            (json.dumps(output, ensure_ascii=False), meta["audited_step_id"]),
        )
        conn.commit()
        meta["expected_total"] = int(conn.execute(EXPECTED_TOTAL_SQL, (meta["job_id"],)).fetchone()[0])
    finally:
        conn.close()
    assert meta["expected_total"] > 0
    return {"db_path": target, **meta}


def test_workflow_summary_is_compact_and_poll_ready(env: dict) -> None:
    with TestClient(create_app(db_path=env["db_path"], start_legacy=False)) as client:
        detail = client.get(f"/api/v1/workflows/{env['workflow_id']}")
        summary = client.get(f"/api/v1/workflows/{env['workflow_id']}/summary")
        assert detail.status_code == 200
        assert summary.status_code == 200
        body = summary.json()
        for key in (
            "workflow_id", "goal_id", "title", "status", "business_outcome", "progress",
            "current_stage", "next_step", "pending_approvals", "recent_artifacts", "recent_events",
        ):
            assert key in body
        assert body["workflow_id"] == env["workflow_id"]
        assert body["status"] == detail.json()["workflow"]["status"]
        assert "business_outcome" in body
        assert set(body["progress"]) == {"completed", "total", "ratio"}
        for key in ("steps", "approvals", "artifacts", "events", "goal", "workflow", "quality"):
            assert key not in body
        for item in body["recent_artifacts"]:
            assert "content" not in item
        print(f"\n[bytes] detail={len(detail.content)} summary={len(summary.content)}")
        assert len(summary.content) < 0.3 * len(detail.content)


def test_workflow_summary_404(env: dict) -> None:
    with TestClient(create_app(db_path=env["db_path"], start_legacy=False)) as client:
        assert client.get("/api/v1/workflows/workflow_missing/summary").status_code == 404


def test_workflow_step_detail_returns_full_output(env: dict) -> None:
    with TestClient(create_app(db_path=env["db_path"], start_legacy=False)) as client:
        response = client.get(f"/api/v1/workflows/{env['workflow_id']}/steps/{env['audited_step_id']}")
        assert response.status_code == 200
        body = response.json()
        assert body["workflow_id"] == env["workflow_id"]
        step = body["step"]
        assert step["id"] == env["audited_step_id"]
        assert step["output"]["audit"]["stdout"] == BIG_STDOUT
        assert isinstance(step["inputs"], dict)
        assert isinstance(step["verification"], dict)
        assert isinstance(step["references"], list)
        detail_step = next(
            item
            for item in client.get(f"/api/v1/workflows/{env['workflow_id']}").json()["steps"]
            if item["id"] == env["audited_step_id"]
        )
        assert detail_step["output"]["_summary_only"] is True
        assert detail_step["output"]["full_detail_available"] is True
        assert "stdout" not in detail_step["output"].get("audit", {})
        assert detail_step["output"]["strategy_v2"]["consultant_judgement"]["version"] == "senior_consultant_v1"
        assert step != detail_step


def test_workflow_step_detail_injects_assessed_items(env: dict) -> None:
    with TestClient(create_app(db_path=env["db_path"], start_legacy=False)) as client:
        response = client.get(f"/api/v1/workflows/{env['workflow_id']}/steps/{env['assessment_step_id']}")
        assert response.status_code == 200
        queue = response.json()["step"]["output"]["assessment_queue"]
        items = queue["assessed_items"]
        assert items
        assert queue["completed"] == len(items)
        assert {"job_candidate_id", "name", "fit_score", "recommendation"} <= set(items[0])


def test_workflow_step_detail_404(env: dict) -> None:
    with TestClient(create_app(db_path=env["db_path"], start_legacy=False)) as client:
        assert client.get(f"/api/v1/workflows/workflow_missing/steps/{env['audited_step_id']}").status_code == 404
        assert client.get(f"/api/v1/workflows/{env['workflow_id']}/steps/99999999").status_code == 404
        assert client.get(f"/api/v1/workflows/{env['workflow_id']}/steps/{env['other_step_id']}").status_code == 404


def test_workflow_candidates_pagination(env: dict) -> None:
    with TestClient(create_app(db_path=env["db_path"], start_legacy=False)) as client:
        first = client.get(f"/api/v1/workflows/{env['workflow_id']}/candidates?limit=5&offset=0")
        assert first.status_code == 200
        first = first.json()
        assert first["total"] == env["expected_total"]
        assert first["limit"] == 5
        assert first["offset"] == 0
        assert len(first["items"]) == min(5, env["expected_total"])
        second = client.get(f"/api/v1/workflows/{env['workflow_id']}/candidates?limit=5&offset=5").json()
        assert not {item["id"] for item in first["items"]} & {item["id"] for item in second["items"]}
        beyond = client.get(f"/api/v1/workflows/{env['workflow_id']}/candidates?limit=5&offset={env['expected_total']}").json()
        assert beyond["items"] == []

        everything = client.get(f"/api/v1/workflows/{env['workflow_id']}/candidates?limit=200").json()["items"]
        assert len(everything) == min(env["expected_total"], 200)
        for item in everything:
            assert set(item) == {
                "id", "person_id", "name", "company", "title", "fit_score", "fit_level",
                "recommendation", "stage", "flow_bucket", "status", "assessed", "attribution", "source_lineage", "updated_at",
                "resume_source_type", "resume_capture_status", "resume_captured_at", "intention",
            }
            name = item["name"]
            assert name and ("*" in name or "某" in name or name.endswith(("先生", "女士", "老师")))
            assert item["fit_score"] is None or 0 <= int(item["fit_score"]) <= 100
            assert item["assessed"] is (item["fit_score"] is not None)
        assert any(item["fit_score"] is not None for item in everything)
        assert any(item["attribution"] for item in everything)
        print(f"\n[candidates] total={env['expected_total']} first_page_bytes={len(json.dumps(first, ensure_ascii=False))}")


def test_mapping_lineage_is_first_class_without_becoming_channel_attribution(env: dict) -> None:
    with TestClient(create_app(db_path=env["db_path"], start_legacy=False)) as client:
        seed_page = client.get(f"/api/v1/workflows/{env['workflow_id']}/candidates?limit=1").json()
        assert seed_page["items"]
        candidate_id = int(seed_page["items"][0]["id"])
    conn = sqlite3.connect(env["db_path"])
    try:
        candidate = conn.execute(
            "SELECT id,person_id FROM job_candidates WHERE id=?",
            (candidate_id,),
        ).fetchone()
        assert candidate
        goal_id = conn.execute("SELECT goal_id FROM agent_workflows WHERE workflow_id=?", (env["workflow_id"],)).fetchone()[0]
        artifact_id = "mapping_lineage_contract_artifact"
        conn.execute(
            """INSERT OR REPLACE INTO agent_artifacts
               (artifact_id,goal_id,workflow_id,artifact_type,title,metadata_json,validation_status)
               VALUES (?,?,?,?,?,?,?)""",
            (artifact_id, goal_id, env["workflow_id"], "mapping_task", "Mapping 直挖任务卡", "{}", "passed"),
        )
        conn.execute(
            """INSERT INTO candidate_events
               (job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (candidate[0], candidate[1], env["job_id"], "mapping_intake", "pending_review", "2026-08-14 10:00:00",
             "Mapping 直挖入库", json.dumps({"mapping_artifact": artifact_id, "candidate_index": 3}), "mapping_task", artifact_id),
        )
        other_artifact_id = "mapping_lineage_other_workflow_artifact"
        other_goal_id = conn.execute(
            "SELECT goal_id FROM agent_workflows WHERE workflow_id=?", (env["page_workflow_id"],)
        ).fetchone()[0]
        conn.execute(
            """INSERT OR REPLACE INTO agent_artifacts
               (artifact_id,goal_id,workflow_id,artifact_type,title,metadata_json,validation_status)
               VALUES (?,?,?,?,?,?,?)""",
            (other_artifact_id, other_goal_id, env["page_workflow_id"], "mapping_task", "另一工作流任务卡", "{}", "passed"),
        )
        conn.execute(
            """INSERT INTO candidate_events
               (job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (candidate[0], candidate[1], env["job_id"], "mapping_intake", "pending_review", "2026-08-14 11:00:00",
             "另一工作流 Mapping 入库", json.dumps({"mapping_artifact": other_artifact_id, "candidate_index": 5}), "mapping_task", other_artifact_id),
        )
        new_person = conn.execute(
            """INSERT INTO people(display_name,current_company,current_title,fingerprint)
               VALUES (?,?,?,?)""",
            ("Mapping 未评估人选", "示例科技", "电源工程师", "mapping-lineage-contract-person"),
        )
        new_relation = conn.execute(
            """INSERT INTO job_candidates
               (job_id,person_id,raw_status,clean_stage,flow_bucket,updated_at)
               VALUES (?,?,?,?,?,?)""",
            (env["job_id"], new_person.lastrowid, "mapping_intake", "S1 新增寻访/待复核", "待复核", "2026-08-14 10:01:00"),
        )
        conn.execute(
            """INSERT INTO candidate_events
               (job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (new_relation.lastrowid, new_person.lastrowid, env["job_id"], "mapping_intake", "pending_review", "2026-08-14 10:01:00",
             "Mapping 未评估人选入库", json.dumps({"mapping_artifact": artifact_id, "candidate_index": 7}), "mapping_task", artifact_id),
        )
        conn.commit()
        unassessed_id = int(new_relation.lastrowid)
    finally:
        conn.close()
    with TestClient(create_app(db_path=env["db_path"], start_legacy=False)) as client:
        first_page = client.get(f"/api/v1/workflows/{env['workflow_id']}/candidates?limit=200").json()
        second_page = client.get(f"/api/v1/workflows/{env['workflow_id']}/candidates?limit=200&offset=200").json()
        items = [*first_page["items"], *second_page["items"]]
        item = next(value for value in items if value["id"] == candidate_id)
        assert item["attribution"]["source_type"] in {"sourcing", "mapping"}
        assert any(lineage.get("artifact_id") == artifact_id for lineage in item["source_lineage"])
        assert not any(lineage.get("artifact_id") == other_artifact_id for lineage in item["source_lineage"])
        detail = client.get(f"/api/v1/candidates/{candidate_id}")
        assert detail.status_code == 200
        assert any(lineage.get("artifact_id") == artifact_id for lineage in detail.json()["candidate"]["source_lineage"])
        assert any(lineage.get("artifact_id") == other_artifact_id for lineage in detail.json()["candidate"]["source_lineage"])
        unassessed = next(value for value in items if value["id"] == unassessed_id)
        assert unassessed["assessed"] is False
        assert unassessed["attribution"]["source_type"] == "mapping"
        assert unassessed["attribution"]["artifact_id"] == artifact_id
        assert unassessed["attribution"]["candidate_index"] == 7


def test_workflow_candidates_404_and_non_job_workflow(env: dict) -> None:
    with TestClient(create_app(db_path=env["db_path"], start_legacy=False)) as client:
        assert client.get("/api/v1/workflows/workflow_missing/candidates").status_code == 404
        response = client.get(f"/api/v1/workflows/{env['page_workflow_id']}/candidates")
        assert response.status_code == 200
        assert response.json()["items"] == []
        assert response.json()["total"] == 0


def test_openapi_lists_new_workflow_routes(env: dict) -> None:
    with TestClient(create_app(db_path=env["db_path"], start_legacy=False)) as client:
        paths = client.get("/openapi.json").json()["paths"]
        assert "/api/v1/workflows/{workflow_id}/summary" in paths
        assert "/api/v1/workflows/{workflow_id}/steps/{step_id}" in paths
        assert "/api/v1/workflows/{workflow_id}/candidates" in paths
