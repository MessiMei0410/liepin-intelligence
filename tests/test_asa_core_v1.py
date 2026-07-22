from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from asa_core.app import create_app
from asa_core.database import migrate
from asa_core.service import _explicit_candidate_update
import liepin_workbench_server as legacy


SOURCE_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")


def test_read_no_reply_update_requires_a_statement_or_write_intent() -> None:
    assert _explicit_candidate_update("记录一下，这个人选消息已读未回") == "read_no_reply"
    assert _explicit_candidate_update("这个人选已读不回") == "read_no_reply"
    assert _explicit_candidate_update("这个人选已读不回怎么办") == ""


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


def test_migrations_are_idempotent(db_path: Path) -> None:
    first = migrate(db_path, backup=False)
    second = migrate(db_path, backup=False)
    assert first["ok"] is True
    assert second["applied"] == []
    assert second["foreign_key_issues"] == []


def test_read_contract_uses_real_v3_data(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        assert client.get("/api/v1/health").status_code == 200
        jobs = client.get("/api/v1/jobs?limit=5").json()
        candidates = client.get("/api/v1/candidates?limit=5").json()
        assert jobs["total"] >= 100
        assert candidates["total"] >= 100
        job_detail = client.get(f"/api/v1/jobs/{jobs['items'][0]['id']}").json()["job"]
        assert job_detail["id"] == jobs["items"][0]["id"]
        assert "position" in job_detail
        assert "profile" in job_detail
        assert "funnel" in job_detail
        assert "stages" in job_detail
        assert "candidates" in job_detail
        assert "search_experiments" in job_detail
        assert "events" in job_detail
        assert "followups" in job_detail
        detail = client.get(f"/api/v1/candidates/{candidates['items'][0]['id']}").json()["candidate"]
        assert "resume" in detail
        assert "source_links" in detail
        assert "job_relations" in detail
        assert "events" in detail


def test_workbench_ui_is_app_only(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        assert client.get("/workbench").status_code == 403
        assert client.get("/asa-app").status_code == 403
        response = client.get("/asa-app", headers={"User-Agent": "ASAApp/0.2.16"})
        assert response.status_code in {200, 503}
        assert client.get("/api/v1/health").status_code == 200


@pytest.mark.parametrize(
    "origin",
    [
        "https://h.liepin.com",
        "https://headhunt.x-saas.com.cn",
    ],
)
def test_candidate_extensions_can_preflight_context_bridge(origin: str, db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = client.options(
            "/api/asa/floating/context",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin


def test_context_bridge_rejects_lookalike_origins(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = client.options(
            "/api/asa/floating/context",
            headers={
                "Origin": "https://h.liepin.com.evil.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert response.status_code == 400
        assert "access-control-allow-origin" not in response.headers


def test_dashboard_and_jobs_exclude_stopped_and_empty_left_join_rows(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        dashboard = client.get("/api/v1/dashboard").json()
        candidates = client.get("/api/v1/candidates?limit=200").json()["items"]
        expected = sum(
            "初筛不通过" not in (item.get("clean_stage") or "")
            and "停止" not in (item.get("clean_stage") or "")
            and (item.get("raw_status") or "").lower()
            not in {"screen_rejected", "xsaas_review_stop", "rejected", "stopped", "closed"}
            for item in candidates
        )
        assert dashboard["counts"]["pending_candidates"] == expected
        jobs = client.get("/api/v1/jobs?limit=200").json()["items"]
        assert all(item["active_candidate_count"] == 0 for item in jobs if item["candidate_count"] == 0)
        assert all(item.get("lifecycle_stage") != "archived" and item.get("status") != "只读快照" for item in jobs)


def test_stopped_candidate_cannot_be_reactivated_by_preflight(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        stopped = next(
            item
            for item in client.get("/api/v1/candidates?limit=200").json()["items"]
            if "初筛不通过" in (item.get("clean_stage") or "")
        )
        detail = client.get(f"/api/v1/candidates/{stopped['id']}").json()["candidate"]
        assert detail["is_stopped"] is True
        response = client.post(
            "/api/v1/candidate-actions/preflight",
            json={"request_id": "stopped-test", "candidate_id": stopped["id"], "action": "advance"},
        )
        assert response.status_code == 409


def test_candidate_event_source_urls_are_available_in_detail(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        detail = client.get("/api/v1/candidates/549").json()["candidate"]
        urls = [item.get("source_url") for item in detail["source_links"] if item.get("source_url")]
        assert any("liepin.com/resume/" in url for url in urls)


def test_candidate_detail_falls_back_to_sourcing_card_profile(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    person_id = conn.execute("SELECT person_id FROM job_candidates WHERE id=563").fetchone()[0]
    conn.execute("DELETE FROM source_profiles WHERE person_id=?", (person_id,))
    conn.execute(
        "DELETE FROM entity_source_links WHERE canonical_type='person' AND canonical_id=?",
        (str(person_id),),
    )
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        detail = client.get("/api/v1/candidates/563").json()["candidate"]
        assert "矽力杰" in detail["resume"]["summary"]
        assert "区域销售经理" in detail["resume"]["full_text"]


def test_migration_backfills_sourcing_card_source_profiles(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    person_id = conn.execute("SELECT person_id FROM job_candidates WHERE id=563").fetchone()[0]
    conn.execute("DELETE FROM source_profiles WHERE person_id=?", (person_id,))
    conn.execute("DELETE FROM schema_migrations WHERE version=4")
    conn.commit()
    conn.close()

    result = migrate(db_path, backup=False)

    assert 4 in result["applied"]
    conn = sqlite3.connect(db_path)
    profile = conn.execute(
        "SELECT source_type,raw_json FROM source_profiles WHERE person_id=? ORDER BY id DESC LIMIT 1",
        (person_id,),
    ).fetchone()
    conn.close()
    assert profile[0] == "liepin"
    assert "矽力杰" in profile[1]


def test_explicit_copilot_review_failure_updates_candidate_and_audit(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE job_candidates SET clean_stage='S1 新增寻访/待复核',flow_bucket='待复核',raw_status='search_shortlisted' WHERE id=558"
    )
    conn.execute("DELETE FROM candidate_events WHERE job_candidate_id=558 AND event_type='resume_review_completed'")
    conn.execute("DELETE FROM agent_sourcing_feedback WHERE job_candidate_id=558")
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        request_id = f"copilot-stop-{uuid.uuid4().hex[:8]}"
        response = client.post(
            "/api/v1/copilot/messages",
            headers={"Idempotency-Key": request_id},
            json={
                "request_id": request_id,
                "session_id": "floating-review-stop-test",
                "message": "这个人选复核不通过",
                "context": {"type": "candidate", "id": 558, "source": "asa_floating"},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["candidate_action"]["action"] == "stop"
        assert body["candidate_action"]["stage"] == "H5 最近寻访/初筛不通过"
        assert "已同步到 ASA" in body["answer"]
        detail = client.get("/api/v1/candidates/558").json()["candidate"]
        assert detail["is_stopped"] is True
        assert detail["clean_stage"] == "H5 最近寻访/初筛不通过"

    conn = sqlite3.connect(db_path)
    event = conn.execute(
        "SELECT event_status,summary FROM candidate_events WHERE job_candidate_id=558 AND event_type='resume_review_completed' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    audit = conn.execute(
        "SELECT operation,surface,result FROM audit_events WHERE target_type='job_candidate' AND target_id='558' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    feedback = conn.execute(
        "SELECT signal_type,weight FROM agent_sourcing_feedback WHERE job_candidate_id=558 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert event[0] == "stop"
    assert "复核不通过" in event[1]
    assert audit == ("candidate.commit", "asa_copilot", "success")
    assert feedback == ("stopped", -2.0)


@pytest.mark.parametrize(
    ("message", "action", "expected_stage", "expected_status", "signal_type", "weight"),
    [
        ("这个人选复核通过", "advance", "S2 复核通过/待联系", "new", "review_pass", 1.0),
        ("这个人选已联系", "contact", "S3 已联系/待回复", "contacted", "contacted", 2.0),
        ("这个人选已推荐给客户", "recommend", "S7 已推荐客户/待反馈", "recommended", "recommended", 3.0),
    ],
)
def test_explicit_copilot_candidate_actions_write_real_business_state(
    db_path: Path,
    message: str,
    action: str,
    expected_stage: str,
    expected_status: str,
    signal_type: str,
    weight: float,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE job_candidates SET clean_stage='S1 新增寻访/待复核',flow_bucket='待复核',raw_status='search_shortlisted',raw_stage='S1 新增寻访/待复核' WHERE id=558"
    )
    conn.execute("UPDATE candidates SET status='new',notes='原始寻访｜query=电气/硬件' WHERE id=1176")
    conn.execute("DELETE FROM candidate_events WHERE job_candidate_id=558 AND event_type IN ('resume_review_completed','candidate_contact_update','candidate_recommended')")
    conn.execute("DELETE FROM agent_sourcing_feedback WHERE job_candidate_id=558")
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        request_id = f"copilot-{action}-{uuid.uuid4().hex[:8]}"
        response = client.post(
            "/api/v1/copilot/messages",
            headers={"Idempotency-Key": request_id},
            json={
                "request_id": request_id,
                "session_id": f"floating-{action}-test",
                "message": message,
                "context": {"type": "candidate", "id": 558, "source": "asa_floating"},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["candidate_action"]["action"] == action
        assert body["candidate_action"]["stage"] == expected_stage
        assert "已同步到 ASA" in body["answer"]
        detail = client.get("/api/v1/candidates/558").json()["candidate"]
        assert detail["clean_stage"] == expected_stage

    conn = sqlite3.connect(db_path)
    candidate_status, candidate_notes = conn.execute("SELECT status,notes FROM candidates WHERE id=1176").fetchone()
    feedback = conn.execute(
        "SELECT signal_type,weight FROM agent_sourcing_feedback WHERE job_candidate_id=558 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    stored_answer = conn.execute(
        "SELECT content FROM agent_copilot_messages WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",
        (f"floating-{action}-test",),
    ).fetchone()[0]
    conn.close()
    assert candidate_status == expected_status
    assert "query=电气/硬件" in candidate_notes
    assert "ASA Copilot 指令" in candidate_notes
    assert feedback == (signal_type, weight)
    assert "已同步到 ASA" in stored_answer


def test_copilot_read_no_reply_updates_note_and_timeline_without_reconfirmation(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE job_candidates SET clean_stage='S3 已联系/待回复',flow_bucket='联系推进',raw_status='contacted' WHERE id=558"
    )
    conn.execute("UPDATE candidates SET status='contacted',notes='原始寻访记录' WHERE id=1176")
    conn.execute(
        "DELETE FROM candidate_events WHERE job_candidate_id=558 AND event_type='candidate_contact_update' AND event_status='read_no_reply'"
    )
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        request_id = f"copilot-read-no-reply-{uuid.uuid4().hex[:8]}"
        response = client.post(
            "/api/v1/copilot/messages",
            headers={"Idempotency-Key": request_id},
            json={
                "request_id": request_id,
                "session_id": "floating-read-no-reply-test",
                "message": "这个人选合适，但是发消息已读不回。这个情况你可以在人选备注页记录下",
                "context": {"type": "candidate", "id": 558, "source": "asa_floating"},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["candidate_update"]["update_type"] == "read_no_reply"
        assert body["candidate_update"]["stage"] == "S3 已联系/待回复"
        assert "已更新" in body["answer"]
        assert "已读未回复" in body["answer"]
        assert "请确认" not in body["answer"]
        assert "打开人选备注" not in body["answer"]
        assert body["suggested_actions"] == []

    conn = sqlite3.connect(db_path)
    candidate = conn.execute("SELECT status,notes FROM candidates WHERE id=1176").fetchone()
    relation = conn.execute("SELECT clean_stage,raw_status FROM job_candidates WHERE id=558").fetchone()
    event = conn.execute(
        "SELECT event_status,summary FROM candidate_events WHERE job_candidate_id=558 AND event_type='candidate_contact_update' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    audit = conn.execute(
        "SELECT operation,surface,result FROM audit_events WHERE target_type='job_candidate' AND target_id='558' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    stored = conn.execute(
        "SELECT content,structured_json FROM agent_copilot_messages WHERE session_id='floating-read-no-reply-test' AND role='assistant' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert candidate[0] == "contacted"
    assert "已读未回复" in candidate[1]
    assert relation == ("S3 已联系/待回复", "contacted")
    assert event[0] == "read_no_reply"
    assert "已读未回复" in event[1]
    assert audit == ("candidate.note", "asa_copilot", "success")
    assert "请确认" not in stored[0]
    assert json.loads(stored[1])["suggested_actions"] == []


def test_copilot_read_no_reply_note_never_downgrades_later_stage(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE job_candidates SET clean_stage='S7 已推荐客户/待反馈',flow_bucket='客户推荐',raw_status='recommended' WHERE id=558"
    )
    conn.execute("UPDATE candidates SET status='recommended' WHERE id=1176")
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        request_id = f"copilot-read-no-reply-later-{uuid.uuid4().hex[:8]}"
        response = client.post(
            "/api/v1/copilot/messages",
            headers={"Idempotency-Key": request_id},
            json={
                "request_id": request_id,
                "session_id": "floating-read-no-reply-later-stage-test",
                "message": "记录一下，这个人选消息已读未回",
                "context": {"type": "candidate", "id": 558, "source": "asa_floating"},
            },
        )
        assert response.status_code == 200
        assert response.json()["candidate_update"]["stage"] == "S7 已推荐客户/待反馈"

    conn = sqlite3.connect(db_path)
    relation = conn.execute("SELECT clean_stage,raw_status FROM job_candidates WHERE id=558").fetchone()
    status = conn.execute("SELECT status FROM candidates WHERE id=1176").fetchone()[0]
    conn.close()
    assert relation == ("S7 已推荐客户/待反馈", "recommended")
    assert status == "recommended"


def test_candidate_action_never_downgrades_a_later_stage(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE job_candidates SET clean_stage='S7 已推荐客户/待反馈',flow_bucket='客户推荐',raw_status='recommended' WHERE id=558"
    )
    conn.execute("DELETE FROM candidate_events WHERE job_candidate_id=558 AND event_type='candidate_contact_update'")
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        preflight = client.post(
            "/api/v1/candidate-actions/preflight",
            json={"request_id": "no-downgrade-preflight", "candidate_id": 558, "action": "contact"},
        ).json()
        response = client.post(
            "/api/v1/candidate-actions/commit",
            headers={"Idempotency-Key": "no-downgrade-commit"},
            json={
                "request_id": "no-downgrade-commit",
                "candidate_id": 558,
                "action": "contact",
                "preflight_token": preflight["token"],
            },
        )
        assert response.status_code == 200
        assert response.json()["already_applied"] is True
        detail = client.get("/api/v1/candidates/558").json()["candidate"]
        assert detail["clean_stage"] == "S7 已推荐客户/待反馈"

    conn = sqlite3.connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM candidate_events WHERE job_candidate_id=558 AND event_type='candidate_contact_update'"
    ).fetchone()[0]
    conn.close()
    assert count == 0


def test_xsaas_review_pass_uses_x2_waiting_manual_contact(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE job_candidates SET clean_stage='X1 X-SaaS新增/待复核',flow_bucket='待复核',raw_status='xsaas_search_shortlisted' WHERE id=558"
    )
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        preflight = client.post(
            "/api/v1/candidate-actions/preflight",
            json={"request_id": "xsaas-pass-preflight", "candidate_id": 558, "action": "advance"},
        ).json()
        response = client.post(
            "/api/v1/candidate-actions/commit",
            headers={"Idempotency-Key": "xsaas-pass-commit"},
            json={
                "request_id": "xsaas-pass-commit",
                "candidate_id": 558,
                "action": "advance",
                "preflight_token": preflight["token"],
            },
        )
        assert response.status_code == 200
        assert response.json()["stage"] == "X2 X-SaaS复核通过/待人工联系"

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT clean_stage,flow_bucket,raw_status FROM job_candidates WHERE id=558"
    ).fetchone()
    conn.close()
    assert row == (
        "X2 X-SaaS复核通过/待人工联系",
        "待人工联系/转猎聘或微信",
        "xsaas_review_continue",
    )


def test_copilot_cannot_reactivate_stopped_candidate(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE job_candidates SET clean_stage='H5 最近寻访/初筛不通过',flow_bucket='最近寻访',raw_status='screen_rejected' WHERE id=558"
    )
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        request_id = f"copilot-stopped-contact-{uuid.uuid4().hex[:8]}"
        response = client.post(
            "/api/v1/copilot/messages",
            headers={"Idempotency-Key": request_id},
            json={
                "request_id": request_id,
                "session_id": "floating-stopped-contact-test",
                "message": "这个人选已联系",
                "context": {"type": "candidate", "id": 558, "source": "asa_floating"},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["write_blocked"] is True
        assert "未写入 ASA" in body["answer"]
        assert client.get("/api/v1/candidates/558").json()["candidate"]["is_stopped"] is True


def test_copilot_review_failure_without_candidate_context_never_claims_write(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        request_id = f"copilot-stop-unresolved-{uuid.uuid4().hex[:8]}"
        response = client.post(
            "/api/v1/copilot/messages",
            headers={"Idempotency-Key": request_id},
            json={
                "request_id": request_id,
                "session_id": "floating-review-stop-unresolved-test",
                "message": "这个人选复核不通过",
                "context": {"type": "global", "source": "asa_floating"},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["write_blocked"] is True
        assert "尚未写入 ASA" in body["answer"]
        assert "已标记" not in body["answer"]


def test_floating_compat_copilot_review_failure_uses_core_write_path(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE job_candidates SET clean_stage='S1 新增寻访/待复核',flow_bucket='待复核',raw_status='search_shortlisted' WHERE id=558"
    )
    conn.execute("DELETE FROM candidate_events WHERE job_candidate_id=558 AND event_type='resume_review_completed'")
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=True)) as client:
        response = client.post(
            "/api/agent/copilot",
            json={
                "session_id": "floating-compat-review-stop-test",
                "message": "这个人选复核不通过",
                "context": {
                    "type": "candidate",
                    "id": 558,
                    "source": "asa_floating",
                    "display_mode": "floating_compact",
                },
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["candidate_action"]["action"] == "stop"
        assert "已同步到 ASA" in body["answer"]
        assert client.get("/api/v1/candidates/558").json()["candidate"]["is_stopped"] is True


def test_candidate_detail_resolves_source_url_written_after_startup(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        conn = sqlite3.connect(db_path)
        person_id = conn.execute("SELECT person_id FROM job_candidates WHERE id=558").fetchone()[0]
        conn.execute(
            "DELETE FROM entity_source_links WHERE canonical_type='person' AND canonical_id=?",
            (str(person_id),),
        )
        resume_url = "https://h.liepin.com/resume/showresumedetail/?res_id_encode=late-event"
        conn.execute(
            """
            INSERT INTO candidate_events
                (job_candidate_id,person_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
            VALUES (558,?,'search_shortlisted','pending_review',datetime('now','localtime'),
                    'late source link',?,'liepin_search',?)
            """,
            (person_id, '{"resume_url":"' + resume_url + '"}', resume_url),
        )
        conn.commit()
        conn.close()

        detail = client.get("/api/v1/candidates/558").json()["candidate"]
        urls = [item.get("source_url") for item in detail["source_links"]]
        assert resume_url in urls


def test_copilot_candidate_synonym_creates_sourcing_workflow(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        request_id = f"copilot-candidate-synonym-{uuid.uuid4().hex[:8]}"
        response = client.post(
            "/api/v1/copilot/messages",
            headers={"Idempotency-Key": request_id},
            json={
                "request_id": request_id,
                "message": "给士兰微技术市场经理再找些候选人",
                "context": {"type": "job", "id": 111},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["workflow_id"]
        assert any(item["type"] == "start_workflow" for item in body["suggested_actions"])
        assert any(step["capability_id"] == "multi_channel_sourcing" for step in body["plan_summary"])


def test_workflow_archive_hides_current_card_but_preserves_detail(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        request_id = f"archive-create-{uuid.uuid4().hex[:8]}"
        created = client.post(
            "/api/v1/copilot/messages",
            headers={"Idempotency-Key": request_id},
            json={
                "request_id": request_id,
                "message": "给士兰微技术市场经理再找些候选人",
                "context": {"type": "job", "id": 111},
            },
        ).json()
        workflow_id = created["workflow_id"]
        assert any(item["workflow_id"] == workflow_id for item in client.get("/api/v1/dashboard").json()["workflows"])

        archive_id = f"archive-{uuid.uuid4().hex[:8]}"
        response = client.post(
            f"/api/v1/workflows/{workflow_id}/archive",
            headers={"Idempotency-Key": archive_id},
            json={"request_id": archive_id},
        )
        assert response.status_code == 200
        assert response.json()["workflow"]["archived_at"]
        assert all(item["workflow_id"] != workflow_id for item in client.get("/api/v1/dashboard").json()["workflows"])
        detail = client.get(f"/api/v1/workflows/{workflow_id}")
        assert detail.status_code == 200
        assert detail.json()["workflow"]["archived_at"]


def test_dashboard_workflows_expose_business_outcome_matching_summary(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        request_id = f"dashboard-outcome-{uuid.uuid4().hex[:8]}"
        created = client.post(
            "/api/v1/copilot/messages",
            headers={"Idempotency-Key": request_id},
            json={
                "request_id": request_id,
                "message": "给士兰微技术市场经理再找些候选人",
                "context": {"type": "job", "id": 111},
            },
        )
        workflow_id = created.json()["workflow_id"]
        # 业务终态生产上由引擎 _finish 写入；此处直接写临时库验证透传链
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE agent_workflows SET business_outcome='completed_needs_review' WHERE workflow_id=?",
            (workflow_id,),
        )
        conn.commit()
        conn.close()

        workflows = client.get("/api/v1/dashboard").json()["workflows"]
        assert all("business_outcome" in item for item in workflows)
        item = next(i for i in workflows if i["workflow_id"] == workflow_id)
        assert item["business_outcome"] == "completed_needs_review"
        summary = client.get(f"/api/v1/workflows/{workflow_id}/summary").json()
        assert item["business_outcome"] == summary["business_outcome"]


def test_copilot_job_split_creates_job_library_update_workflow(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        request_id = f"copilot-job-library-update-{uuid.uuid4().hex[:8]}"
        response = client.post(
            "/api/v1/copilot/messages",
            headers={"Idempotency-Key": request_id},
            json={
                "request_id": request_id,
                "message": "把士兰微技术市场经理/总监分成 PC、服务器、ADAS 三个岗位",
                "context": {"type": "page", "page": "jobs"},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["workflow_id"]
        assert any(item["type"] == "start_workflow" for item in body["suggested_actions"])
        assert any(step["capability_id"] == "job_library_update" for step in body["plan_summary"])


def test_job_library_update_uses_position_profiles_and_archives_legacy_split_job(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        runtime = client.app.state.core.agent_service.capability_runtime
        result = runtime.run_job_library_update(
            {"type": "page", "page": "jobs"},
            {
                "objective": "把士兰微技术市场经理/总监分成 PC、服务器、ADAS 三个岗位",
                "skip_sync": True,
            },
        )
        update = result["job_library_update"]
        titles = {item["title"] for item in update["changes"]}
        assert "技术市场经理/总监（PC电源）" in titles
        assert "技术市场经理/总监（服务器三次电源）" in titles
        assert "技术市场经理/总监（ADAS电源）" in titles
        assert update["archived_legacy"]

    conn = sqlite3.connect(db_path)
    try:
        job_rows = conn.execute(
            """
            SELECT j.title,j.status,j.lifecycle_stage,m.priority
            FROM jobs j JOIN clients c ON c.id=j.client_id
            LEFT JOIN job_pipeline_metrics m ON m.job_id=j.id
            WHERE c.name='士兰微' AND j.title IN (
              '技术市场经理/总监（PC电源）',
              '技术市场经理/总监（服务器三次电源）',
              '技术市场经理/总监（ADAS电源）'
            )
            """
        ).fetchall()
        assert len(job_rows) == 3
        assert all("P0-最急" in (row[3] or "") for row in job_rows)
        legacy = conn.execute(
            """
            SELECT status,lifecycle_stage FROM jobs j JOIN clients c ON c.id=j.client_id
            WHERE c.name='士兰微' AND j.title LIKE '%服务器或PC市场%'
            """
        ).fetchall()
        assert legacy
        assert all(row[1] == "archived" for row in legacy)
    finally:
        conn.close()


def test_candidate_write_is_idempotent_and_preflight_is_single_use(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        candidate_id = next(
            item["id"]
            for item in client.get("/api/v1/candidates?limit=200").json()["items"]
            if "初筛不通过" not in (item.get("clean_stage") or "")
            and (item.get("raw_status") or "").lower() not in {"screen_rejected", "xsaas_review_stop"}
        )
        preflight = client.post(
            "/api/v1/candidate-actions/preflight",
            json={"request_id": "test-preflight", "candidate_id": candidate_id, "action": "review"},
        ).json()
        body = {
            "request_id": "test-commit",
            "candidate_id": candidate_id,
            "action": "review",
            "preflight_token": preflight["token"],
        }
        headers = {"Idempotency-Key": f"review-{candidate_id}"}
        first = client.post("/api/v1/candidate-actions/commit", json=body, headers=headers)
        replay = client.post("/api/v1/candidate-actions/commit", json=body, headers=headers)
        reused = client.post(
            "/api/v1/candidate-actions/commit",
            json={**body, "request_id": "test-reused"},
            headers={"Idempotency-Key": f"review-{candidate_id}-reused"},
        )
        assert first.status_code == 200
        assert replay.status_code == 200
        assert replay.json()["receipt"]["idempotent_replay"] is True
        assert reused.status_code == 409


def test_user_business_actions_update_sourcing_keyword_memory(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE job_candidates SET clean_stage='S1 新增寻访/待复核',flow_bucket='待复核',raw_status='search_shortlisted' WHERE id=558"
        )
        conn.execute("DELETE FROM candidate_events WHERE job_candidate_id=558 AND event_type='resume_review_completed'")
        conn.execute("DELETE FROM agent_sourcing_feedback WHERE job_candidate_id=558")
        conn.execute("DELETE FROM agent_sourcing_attributions WHERE job_candidate_id=558")
        conn.execute("DELETE FROM agent_memories WHERE source_type='sourcing_performance'")
        conn.commit()
        conn.close()

        for action in ("advance", "contact", "recommend"):
            preflight = client.post(
                "/api/v1/candidate-actions/preflight",
                json={"request_id": f"preflight-{action}", "candidate_id": 558, "action": action},
            ).json()
            request_id = f"commit-{action}-{uuid.uuid4().hex[:6]}"
            response = client.post(
                "/api/v1/candidate-actions/commit",
                headers={"Idempotency-Key": request_id},
                json={
                    "request_id": request_id,
                    "candidate_id": 558,
                    "action": action,
                    "preflight_token": preflight["token"],
                },
            )
            assert response.status_code == 200
            assert response.json()["sourcing_learning"]["recorded"] is True

        detail = client.get("/api/v1/candidates/558").json()["candidate"]
        assert detail["clean_stage"] == "S7 已推荐客户/待反馈"
        assert detail["sourcing_attributions"][0]["source_query"] == "电气/硬件"
        assert detail["sourcing_attributions"][0]["learning_score"] == 6.0
        assert detail["sourcing_attributions"][0]["review_pass_count"] == 1
        assert detail["sourcing_attributions"][0]["contacted_count"] == 1
        assert detail["sourcing_attributions"][0]["recommended_count"] == 1

        agent = client.app.state.core.agent_service
        client_signal = agent.record_sourcing_business_signal(
            558, "client_rejected", actor_type="client", note="方向不符",
            source_type="client_feedback_event", source_id="test-client-feedback",
        )
        assert client_signal["recorded"] is True
        updated = client.get("/api/v1/candidates/558").json()["candidate"]["sourcing_attributions"][0]
        assert updated["learning_score"] == 3.0
        assert updated["client_rejected_count"] == 1

        conn = sqlite3.connect(db_path)
        memory = conn.execute(
            "SELECT content FROM agent_memories WHERE source_type='sourcing_performance'"
        ).fetchone()
        conn.close()
        assert memory is not None
        assert "联系 1" in memory[0]
        assert "推荐 1" in memory[0]
        assert "客户否决 1" in memory[0]


def test_client_feedback_updates_sourcing_learning(db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM agent_sourcing_feedback WHERE job_candidate_id=558")
    conn.commit()
    conn.close()
    state = legacy.WorkbenchState(db_path, tmp_path, "127.0.0.1", 8765)
    state.run_refresh = lambda: {"ok": True}  # type: ignore[method-assign]
    monkeypatch.setattr(legacy, "refresh_a_system_workbench", lambda: {"ok": True})
    try:
        result = legacy.write_client_feedback(
            state,
            {
                "write": True,
                "candidate_id": 1176,
                "job_candidate_id": 558,
                "candidate_name": "黄**",
                "candidate_company": "奇瑞汽车股份有限公司",
                "client": "士兰微",
                "position": "技术市场经理（三次电源/服务器或PC市场）",
                "feedback_type": "approved",
                "feedback_detail": "客户认可技术背景",
            },
        )
        assert result["sourcing_learning"]["recorded"] is True
        assert result["sourcing_learning"]["signal_type"] == "client_approved"
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            """
            SELECT SUM(sf.weight),SUM(sf.signal_type='client_approved')
            FROM agent_sourcing_feedback sf
            WHERE sf.job_candidate_id=558
            """
        ).fetchone()
        conn.close()
        assert row == (4.0, 1)
    finally:
        state.close()
