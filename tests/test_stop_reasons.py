from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from _local import env_path, require_local

import pytest
from fastapi.testclient import TestClient

from asa_core.app import create_app
from asa_core.database import ensure_stop_reason_schema, migrate
from asa_core.stop_reasons import STOP_REASON_LABELS, normalize_stop_reason
import liepin_workbench_server as legacy


SOURCE_DB = env_path("ASA_SOURCE_DB", Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db"))
STOP_STATUSES = {"screen_rejected", "xsaas_review_stop", "rejected", "stopped", "closed"}
STOP_STAGE_TOKENS = ("初筛不通过", "停止", "淘汰", "关闭")


@pytest.fixture(scope="module")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # 模块级共享副本：整模块只复制一次生产库（1.5GB）。各测试先复位候选人 558；
    # 依赖"整表停止原因全 NULL"的 summary/schema 测试在测试内再次清洗。
    target = tmp_path_factory.mktemp("stop-reasons") / "asa.db"
    require_local(SOURCE_DB, "正式库 talent_system_v3")
    source = sqlite3.connect(SOURCE_DB)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    # R10 上线后生产库会产生真实 stop_reason 写入；本套测试的"历史数据"语义基于该列全 NULL，
    # 在临时副本中清洗（绝不动源库）
    conn = sqlite3.connect(target)
    try:
        conn.execute("UPDATE job_candidates SET stop_reason=NULL WHERE stop_reason IS NOT NULL")
        conn.commit()
    finally:
        conn.close()
    return target


def _reset_candidate_558(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE job_candidates SET clean_stage='S1 新增寻访/待复核',flow_bucket='待复核',"
        "raw_status='search_shortlisted',raw_stage='S1 新增寻访/待复核',stop_reason=NULL WHERE id=558"
    )
    conn.execute("DELETE FROM candidate_events WHERE job_candidate_id=558 AND event_type='resume_review_completed'")
    conn.execute("DELETE FROM agent_sourcing_feedback WHERE job_candidate_id=558")
    conn.commit()
    conn.close()


def _clear_all_stop_reasons(db_path: Path) -> None:
    """共享副本：依赖"已停止候选人全部未标注"整表语义的测试先清空 stop_reason。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE job_candidates SET stop_reason=NULL WHERE stop_reason IS NOT NULL")
        conn.commit()
    finally:
        conn.close()


def _active_candidate_ids(client: TestClient, count: int) -> list[int]:
    items = client.get("/api/v1/candidates?limit=200").json()["items"]
    active = [
        item["id"]
        for item in items
        if not any(token in (item.get("clean_stage") or "") for token in STOP_STAGE_TOKENS)
        and (item.get("raw_status") or "").lower() not in STOP_STATUSES
    ]
    assert len(active) >= count
    return active[:count]


def _commit_stop(client: TestClient, candidate_id: int, note: str = "", reason: str = ""):
    preflight = client.post(
        "/api/v1/candidate-actions/preflight",
        json={"request_id": f"pf-{uuid.uuid4().hex[:8]}", "candidate_id": candidate_id, "action": "stop"},
    )
    assert preflight.status_code == 200, preflight.text
    request_id = f"stop-{uuid.uuid4().hex[:8]}"
    payload = {
        "request_id": request_id,
        "candidate_id": candidate_id,
        "action": "stop",
        "preflight_token": preflight.json()["token"],
    }
    if note:
        payload["note"] = note
    if reason:
        payload["reason"] = reason
    return client.post("/api/v1/candidate-actions/commit", json=payload, headers={"Idempotency-Key": request_id})


def test_normalize_stop_reason_rules() -> None:
    assert normalize_stop_reason("too_senior", "备注") == ("too_senior", "备注")
    assert normalize_stop_reason(" TOO_SENIOR ", "备注") == ("too_senior", "备注")
    assert normalize_stop_reason("", "备注") == ("other", "备注")
    assert normalize_stop_reason(None, "备注") == ("other", "备注")
    # A 系统行内复核旧码别名归一到统一枚举
    assert normalize_stop_reason("salary_high", "备注") == ("salary_mismatch", "备注")
    assert normalize_stop_reason("duplicate", "") == ("duplicate_candidate", "")
    assert normalize_stop_reason("随便填的", "") == ("other", "停止原因：随便填的")
    code, note = normalize_stop_reason("随便填的", "已有备注")
    assert code == "other"
    assert "已有备注" in note and "随便填的" in note


def test_all_stop_reason_enums_are_accepted(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        candidate_ids = _active_candidate_ids(client, len(STOP_REASON_LABELS))
        for (code, _label), candidate_id in zip(STOP_REASON_LABELS.items(), candidate_ids):
            response = _commit_stop(client, candidate_id, note="备注", reason=code)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["stop_reason"] == code
            assert body["stop_reason_label"] == STOP_REASON_LABELS[code]
    conn = sqlite3.connect(db_path)
    try:
        placeholders = ",".join("?" for _ in candidate_ids)
        stored = {
            row[0]: row[1]
            for row in conn.execute(
                f"SELECT id, stop_reason FROM job_candidates WHERE id IN ({placeholders})", tuple(candidate_ids)
            )
        }
        for (code, _label), candidate_id in zip(STOP_REASON_LABELS.items(), candidate_ids):
            assert stored[candidate_id] == code
        event_raw = conn.execute(
            "SELECT raw_json FROM candidate_events WHERE job_candidate_id=? AND event_type='resume_review_completed'"
            " ORDER BY id DESC LIMIT 1",
            (candidate_ids[0],),
        ).fetchone()[0]
    finally:
        conn.close()
    assert json.loads(event_raw)["stop_reason"] == next(iter(STOP_REASON_LABELS))


def test_unknown_stop_reason_degrades_to_other_and_preserves_text(db_path: Path) -> None:
    _reset_candidate_558(db_path)
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = _commit_stop(client, 558, note="原有备注", reason="客户觉得太贵了")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["stop_reason"] == "other"
        assert body["stop_reason_label"] == "其他"
    conn = sqlite3.connect(db_path)
    try:
        stop_reason, clean_reason = conn.execute(
            "SELECT stop_reason, clean_reason FROM job_candidates WHERE id=558"
        ).fetchone()
        summary, raw_json = conn.execute(
            "SELECT summary, raw_json FROM candidate_events WHERE job_candidate_id=558"
            " AND event_type='resume_review_completed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert stop_reason == "other"
    assert "原有备注" in clean_reason and "客户觉得太贵了" in clean_reason
    assert "客户觉得太贵了" in summary
    assert json.loads(raw_json)["stop_reason"] == "other"


def test_note_only_payload_behaves_as_before(db_path: Path) -> None:
    _reset_candidate_558(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE candidates SET notes='原始寻访' WHERE id=1176")
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = _commit_stop(client, 558, note="自由文本备注")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["stop_reason"] == "other"
        assert body["stage"] == "H5 最近寻访/初筛不通过"
        detail = client.get("/api/v1/candidates/558").json()["candidate"]
        assert detail["is_stopped"] is True
        assert detail["stop_reason"] == "自由文本备注"  # 旧语义：备注文本
        assert detail["stop_reason_code"] == "other"
        assert detail["stop_reason_label"] == "其他"

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT stop_reason, clean_reason, raw_status FROM job_candidates WHERE id=558").fetchone()
        legacy_notes = conn.execute("SELECT notes FROM candidates WHERE id=1176").fetchone()[0]
    finally:
        conn.close()
    assert row == ("other", "自由文本备注", "screen_rejected")
    assert "原始寻访" in legacy_notes and "自由文本备注" in legacy_notes


def test_repeat_stop_still_returns_409(db_path: Path) -> None:
    _reset_candidate_558(db_path)
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        first = _commit_stop(client, 558, reason="too_senior")
        assert first.status_code == 200, first.text
        again = client.post(
            "/api/v1/candidate-actions/preflight",
            json={"request_id": "again-preflight", "candidate_id": 558, "action": "stop"},
        )
        assert again.status_code == 409


def test_stop_reasons_summary_counts_and_unlabeled(db_path: Path) -> None:
    # 前序测试已为多个候选人写入 stop_reason；"存量停止行全部未标注"的整表断言
    # 需要先清空 stop_reason（清的是临时副本，绝不写生产库）。
    _clear_all_stop_reasons(db_path)
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        before = client.get("/api/v1/candidates/stop-reasons/summary").json()
        assert before["ok"] is True
        assert {item["reason"] for item in before["items"]} == set(STOP_REASON_LABELS)
        for item in before["items"]:
            assert item["label"] == STOP_REASON_LABELS[item["reason"]]
        # 历史数据不迁移：存量停止行全部归入"未标注"
        assert before["unlabeled"]["label"] == "未标注"
        assert before["unlabeled"]["count"] == before["total_stopped"]
        counts_before = {item["reason"]: item["count"] for item in before["items"]}

        candidate_ids = _active_candidate_ids(client, 3)
        assert _commit_stop(client, candidate_ids[0], reason="too_senior").status_code == 200
        assert _commit_stop(client, candidate_ids[1], reason="low_intent").status_code == 200
        assert _commit_stop(client, candidate_ids[2], note="仅备注").status_code == 200

        after = client.get("/api/v1/candidates/stop-reasons/summary").json()
        counts_after = {item["reason"]: item["count"] for item in after["items"]}
        assert counts_after["too_senior"] == counts_before["too_senior"] + 1
        assert counts_after["low_intent"] == counts_before["low_intent"] + 1
        assert counts_after["other"] == counts_before["other"] + 1
        assert after["unlabeled"]["count"] == before["unlabeled"]["count"]
        assert after["total_stopped"] == before["total_stopped"] + 3

        # 静态路由不被 {candidate_id} 吞掉，候选人详情仍可用
        detail = client.get(f"/api/v1/candidates/{candidate_ids[0]}")
        assert detail.status_code == 200
        assert detail.json()["candidate"]["stop_reason_code"] == "too_senior"


def test_stop_reason_schema_ensure_is_idempotent(db_path: Path) -> None:
    # 前序测试已写入 stop_reason；"存量行全部 NULL"断言需先清空 stop_reason。
    _clear_all_stop_reasons(db_path)
    first = migrate(db_path, backup=False)
    assert first["ok"] is True
    conn = sqlite3.connect(db_path)
    try:
        ensure_stop_reason_schema(conn)  # 重复调用不报错
        ensure_stop_reason_schema(conn)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(job_candidates)")]
        assert columns.count("stop_reason") == 1
        # 历史数据不迁移：存量行 stop_reason 保持 NULL
        nulls = conn.execute("SELECT COUNT(*) FROM job_candidates WHERE stop_reason IS NULL").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM job_candidates").fetchone()[0]
        assert nulls == total
    finally:
        conn.close()
    second = migrate(db_path, backup=False)
    assert second["applied"] == []


def test_legacy_build_action_normalizes_stop_reason() -> None:
    action = legacy.build_talent_action(
        {
            "kind": "xsaas_review",
            "review_result": "stop",
            "summary": "X-SaaS插件复核：stop",
            "job_candidate_id": 1,
            "stop_reason_code": "salary_mismatch",
            "stop_reason_note": "期望 80 万",
        }
    )
    assert action["stop_reason_code"] == "salary_mismatch"
    assert action["stop_reason_note"] == "期望 80 万"

    degraded = legacy.build_talent_action(
        {
            "kind": "xsaas_review",
            "review_result": "stop",
            "summary": "X-SaaS插件复核：stop",
            "job_candidate_id": 1,
            "stop_reason_code": "薪资太贵",
            "stop_reason_note": "",
        }
    )
    assert degraded["stop_reason_code"] == "other"
    assert "薪资太贵" in degraded["stop_reason_note"]

    continued = legacy.build_talent_action(
        {
            "kind": "xsaas_review",
            "review_result": "continue",
            "summary": "X-SaaS插件复核：continue",
            "job_candidate_id": 1,
            "stop_reason_code": "too_senior",
        }
    )
    assert continued["stop_reason_code"] == "too_senior"  # 非停止不改动转发语义


def test_legacy_persist_stop_reason_writes_column_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "talent.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE job_candidates(id INTEGER PRIMARY KEY, raw_status TEXT)")
    conn.execute("INSERT INTO job_candidates(id, raw_status) VALUES (1, 'xsaas_review_stop')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(legacy, "TALENT_DB", db)

    payload = {
        "kind": "xsaas_review",
        "review_result": "stop",
        "stop_reason_code": "direction_mismatch",
        "job_candidate_id": 1,
    }
    legacy.persist_talent_action_stop_reason(payload)
    legacy.persist_talent_action_stop_reason(payload)  # 幂等
    # 非停止动作与未知 kind 不写
    legacy.persist_talent_action_stop_reason({"kind": "xsaas_review", "review_result": "continue", "job_candidate_id": 1})
    legacy.persist_talent_action_stop_reason({"kind": "candidate_intake", "job_candidate_id": 1})

    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT stop_reason FROM job_candidates WHERE id=1").fetchone()
        columns = [r[1] for r in conn.execute("PRAGMA table_info(job_candidates)")]
    finally:
        conn.close()
    assert row == ("direction_mismatch",)
    assert columns.count("stop_reason") == 1
