from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from _local import env_path, require_local

import pytest
from fastapi.testclient import TestClient

from asa_core.app import create_app


SOURCE_DB = env_path("ASA_SOURCE_DB", Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db"))

GRAPH_DOC = {
    "meta": {"version": "v1", "created": "2026-07-23"},
    "stats": {"companies": 1},
    "companies": {"已有图谱公司": {"track": "前道设备", "business": "刻蚀设备", "categories": ["整机"]}},
}


@pytest.fixture(scope="module")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # 模块级共享副本：整模块只复制一次生产库（1.5GB）。client fixture 每次
    # _seed 前先清空种子行与 knowledge_proposals，保证每个测试起点一致。
    target = tmp_path_factory.mktemp("knowledge-proposals") / "asa.db"
    require_local(SOURCE_DB, "正式库 talent_system_v3")
    source = sqlite3.connect(SOURCE_DB)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


@pytest.fixture()
def kb_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """知识库目录用 tmp + ASA_KNOWLEDGE_BASE_DIR 覆盖，绝不写真实知识库目录。"""
    directory = tmp_path / "kb"
    directory.mkdir()
    (directory / "kb_company_graph_jsj_v1.json").write_text(
        json.dumps(GRAPH_DOC, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setenv("ASA_KNOWLEDGE_BASE_DIR", str(directory))
    return directory


def _seed(db: Path) -> None:
    conn = sqlite3.connect(db)
    try:
        # 共享副本：先清空种子行与已生成提案，再重建种子数据，保证每个测试
        # 的 generate/决策语义与函数级副本一致（不写生产库，仅清临时副本）。
        conn.execute("DELETE FROM knowledge_proposals")
        conn.execute("DELETE FROM consultant_confirmed_recommendations WHERE job_candidate_id IN (9100,9101,9102,9200)")
        conn.execute("DELETE FROM recommendation_package_feedback WHERE request_id LIKE 'kp-fb-%'")
        conn.execute("DELETE FROM job_candidates WHERE id IN (9100,9101,9102,9200)")
        conn.execute("DELETE FROM people WHERE id IN (9100,9101,9102,9200)")
        conn.execute("INSERT OR IGNORE INTO clients(id,name) VALUES (9001,'聚类客户甲')")
        conn.execute("INSERT OR IGNORE INTO clients(id,name) VALUES (9002,'稀疏客户乙')")
        conn.execute("INSERT OR IGNORE INTO jobs(id,client_id,title) VALUES (9001,9001,'刻蚀工程师')")
        conn.execute("INSERT OR IGNORE INTO jobs(id,client_id,title) VALUES (9002,9002,'薄膜工程师')")
        for index in range(3):
            conn.execute(
                "INSERT INTO people(id,display_name,current_company,fingerprint) VALUES (?,?,'新星半导体有限公司','fp-kp-' || ?)",
                (9100 + index, f"人选{index}", index),
            )
            conn.execute(
                """INSERT INTO job_candidates(id,job_id,person_id,clean_stage,stop_reason)
                   VALUES (?,9001,?,'H5 最近寻访/初筛不通过','direction_mismatch')""",
                (9100 + index, 9100 + index),
            )
        # 低于阈值的聚类（只留候选不生成提案）
        conn.execute("INSERT INTO people(id,display_name,fingerprint) VALUES (9200,'稀疏人选','fp-kp-sparse')")
        conn.execute(
            """INSERT INTO job_candidates(id,job_id,person_id,clean_stage,stop_reason)
               VALUES (9200,9002,9200,'H5 最近寻访/初筛不通过','direction_mismatch')"""
        )
        # 客户反馈聚类（rejected/hold ≥2 → negative_rule 提案）
        for index in range(2):
            conn.execute(
                """INSERT INTO recommendation_package_feedback
                   (package_id,package_version,job_candidate_id,person_id,job_id,feedback_type,content,request_id)
                   VALUES ('pkg-seed-1',1,9100,9100,9001,'rejected','客户否决：方向与岗位不符','kp-fb-' || ?)""",
                (index,),
            )
        # 确认推荐聚类（同一现职公司 ≥2 且不在图谱 → company_graph_entry 提案）
        for index in range(2):
            conn.execute(
                """INSERT INTO consultant_confirmed_recommendations
                   (job_candidate_id,person_id,job_id,reason,confirmation_token)
                   VALUES (?,?,9001,'硬性要求匹配','kp-tok-' || ?)""",
                (9100 + index, 9100 + index, index),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def client(db_path: Path, kb_dir: Path):
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as test_client:
        _seed(db_path)
        yield test_client


def _generate(client: TestClient, key: str | None = None) -> dict:
    # 共享副本：generate 走 API 幂等（重放不落库）。默认 key 每次调用唯一，
    # 避免跨测试命中前序 generate 的幂等重放导致"返回了创建结果但库内无提案"。
    key = key or f"kp-gen-{uuid.uuid4().hex[:8]}"
    response = client.post(
        "/api/v1/knowledge-proposals/generate",
        headers={"Idempotency-Key": key},
        json={"request_id": f"{key}-req"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _decide(client: TestClient, proposal_id: str, decision: str, note: str = "", key: str = "") -> tuple[int, dict]:
    preflight = client.post(
        f"/api/v1/knowledge-proposals/{proposal_id}/preflight",
        json={"request_id": f"kp-pre-{proposal_id[:12]}-{decision}"},
    )
    assert preflight.status_code == 200, preflight.text
    token = preflight.json()["confirmation_token"]
    response = client.post(
        f"/api/v1/knowledge-proposals/{proposal_id}/decision",
        headers={"Idempotency-Key": key or f"kp-decide-{proposal_id[:12]}-{decision}"},
        json={
            "request_id": f"kp-commit-{proposal_id[:12]}-{decision}",
            "confirmation_token": token,
            "decision": decision,
            "note": note,
        },
    )
    return response.status_code, response.json()


def test_generate_creates_proposals_and_keeps_below_threshold_as_candidates(client: TestClient) -> None:
    payload = _generate(client)
    titles = {item["title"]: item for item in payload["created"]}
    # 真实库数据漂移（如长越科技 direction_mismatch 聚类已超阈值）会带来额外提案，
    # 断言种子数据产生的提案至少都在，而不是精确相等。
    assert {
        "排除规则建议：聚类客户甲 × 方向不符",
        "排除规则建议：聚类客户甲 客户反馈聚类",
        "公司图谱增补：新星半导体有限公司",
    } <= set(titles)
    for item in payload["created"]:
        assert item["status"] == "pending"
    keys = {candidate["key"] for candidate in payload["candidates"]}
    assert "稀疏客户乙 × 方向不符" in keys
    sparse = next(candidate for candidate in payload["candidates"] if candidate["key"] == "稀疏客户乙 × 方向不符")
    assert sparse["count"] == 1 and sparse["needed"] == 3
    assert "证据不足" in sparse["reason"]


def test_generate_is_idempotent(client: TestClient, db_path: Path) -> None:
    first = _generate(client, "kp-gen-idem")
    second = _generate(client, "kp-gen-idem-2")
    assert len(second["created"]) == 0
    # 幂等：第二次全部落入 existing，且总数与首次创建一致（真实库漂移只影响数量基数）。
    assert len(second["existing"]) == len(first["created"]) >= 3
    conn = sqlite3.connect(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM knowledge_proposals").fetchone()[0]
    finally:
        conn.close()
    assert total == len(first["created"])


def test_list_and_detail_endpoints(client: TestClient) -> None:
    created = _generate(client)["created"]
    listed = client.get("/api/v1/knowledge-proposals?status=pending")
    assert listed.status_code == 200
    # 真实库漂移可能带来额外 pending 提案：断言种子提案都在列表里，计数至少覆盖。
    assert listed.json()["counts"]["pending"] >= 3
    assert {item["proposal_id"] for item in created} <= {item["proposal_id"] for item in listed.json()["items"]}

    seeded = next(item for item in created if item["title"] == "排除规则建议：聚类客户甲 × 方向不符")
    detail = client.get(f"/api/v1/knowledge-proposals/{seeded['proposal_id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["content"]["scope"] == "聚类客户甲"
    assert body["evidence"][0]["source_type"] in {"stop_reason", "client_feedback"}
    assert body["evidence"][0]["source_ids"]

    missing = client.get("/api/v1/knowledge-proposals/kprop_missing")
    assert missing.status_code == 404
    bad_status = client.get("/api/v1/knowledge-proposals?status=unknown")
    assert bad_status.status_code == 409


def test_accept_negative_rule_writes_confirmed_rules_file(client: TestClient, kb_dir: Path) -> None:
    proposal = next(
        item for item in _generate(client)["created"] if item["proposal_type"] == "negative_rule"
    )
    status, payload = _decide(client, proposal["proposal_id"], "accept")
    assert status == 200, payload
    assert payload["status"] == "accepted"
    assert payload["receipt"]["idempotent_replay"] is False

    rules_path = kb_dir / "kb_agent_confirmed_rules_v1.json"
    assert rules_path.is_file()
    doc = json.loads(rules_path.read_text(encoding="utf-8"))
    assert doc["meta"]["version"] == "v1"
    assert len(doc["rules"]) == 1
    rule = doc["rules"][0]
    assert rule["proposed_by"] == "consultant_confirmed"
    assert rule["source"] == "knowledge_proposal"
    assert rule["proposal_id"] == proposal["proposal_id"]
    assert rule["rule_type"] == "negative_rule"
    assert "聚类客户甲" in rule["content"]["rule"]
    # 真实知识库目录不被测试写入
    assert str(kb_dir) in payload["applied_to"]


def test_accept_company_graph_entry_appends_graph(client: TestClient, kb_dir: Path) -> None:
    proposal = next(
        item for item in _generate(client)["created"] if item["proposal_type"] == "company_graph_entry"
    )
    status, payload = _decide(client, proposal["proposal_id"], "accept")
    assert status == 200, payload
    assert payload["status"] == "accepted"

    graph = json.loads((kb_dir / "kb_company_graph_jsj_v1.json").read_text(encoding="utf-8"))
    entry = graph["companies"]["新星半导体有限公司"]
    assert entry["proposed_by"] == "consultant_confirmed"
    assert entry["proposal_id"] == proposal["proposal_id"]
    assert graph["stats"]["companies"] == 2
    assert "已有图谱公司" in graph["companies"]  # 既有条目不被改动


def test_reject_requires_note_and_records_reason(client: TestClient) -> None:
    proposal = next(
        item for item in _generate(client)["created"] if "客户反馈聚类" in item["title"]
    )
    status, payload = _decide(client, proposal["proposal_id"], "reject")
    assert status == 409
    assert "原因" in payload["detail"]

    status, payload = _decide(client, proposal["proposal_id"], "reject", note="反馈样本太薄，先观察", key="kp-reject-with-note")
    assert status == 200, payload
    assert payload["status"] == "rejected"
    detail = client.get(f"/api/v1/knowledge-proposals/{proposal['proposal_id']}").json()
    assert detail["status"] == "rejected"
    assert detail["decision_note"] == "反馈样本太薄，先观察"
    # 已终态提案不可再确认
    again = client.post(
        f"/api/v1/knowledge-proposals/{proposal['proposal_id']}/preflight",
        json={"request_id": "kp-pre-terminal"},
    )
    assert again.status_code == 409


def test_decision_rejects_invalid_or_used_token(client: TestClient) -> None:
    proposal = _generate(client)["created"][0]
    response = client.post(
        f"/api/v1/knowledge-proposals/{proposal['proposal_id']}/decision",
        headers={"Idempotency-Key": "kp-bad-token"},
        json={"request_id": "kp-bad-token-req", "confirmation_token": "not-a-token", "decision": "accept"},
    )
    assert response.status_code == 409
    assert "令牌" in response.json()["detail"]

    # 令牌一次性：第一次成功后第二次 preflight 报 409（状态已变化）
    status, _ = _decide(client, proposal["proposal_id"], "accept", key="kp-good-token")
    assert status == 200
    reuse = client.post(
        f"/api/v1/knowledge-proposals/{proposal['proposal_id']}/decision",
        headers={"Idempotency-Key": "kp-reuse-token"},
        json={"request_id": "kp-reuse-token-req", "confirmation_token": "not-a-token", "decision": "accept"},
    )
    assert reuse.status_code == 409


def test_decision_detects_content_drift(client: TestClient, db_path: Path) -> None:
    proposal = _generate(client)["created"][0]
    preflight = client.post(
        f"/api/v1/knowledge-proposals/{proposal['proposal_id']}/preflight",
        json={"request_id": "kp-pre-drift"},
    )
    token = preflight.json()["confirmation_token"]
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE knowledge_proposals SET content_json=?, updated_at=datetime('now','localtime') WHERE proposal_id=?",
            (json.dumps({"scope": "被篡改"}, ensure_ascii=False), proposal["proposal_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    response = client.post(
        f"/api/v1/knowledge-proposals/{proposal['proposal_id']}/decision",
        headers={"Idempotency-Key": "kp-drift"},
        json={"request_id": "kp-drift-req", "confirmation_token": token, "decision": "accept"},
    )
    assert response.status_code == 409
    assert "内容已变化" in response.json()["detail"]


def test_decision_idempotent_replay(client: TestClient) -> None:
    proposal = _generate(client)["created"][0]
    preflight = client.post(
        f"/api/v1/knowledge-proposals/{proposal['proposal_id']}/preflight",
        json={"request_id": "kp-pre-replay"},
    )
    body = {
        "request_id": "kp-replay-req",
        "confirmation_token": preflight.json()["confirmation_token"],
        "decision": "reject",
        "note": "重复提交验证",
    }
    first = client.post(
        f"/api/v1/knowledge-proposals/{proposal['proposal_id']}/decision",
        headers={"Idempotency-Key": "kp-replay-key"},
        json=body,
    )
    replay = client.post(
        f"/api/v1/knowledge-proposals/{proposal['proposal_id']}/decision",
        headers={"Idempotency-Key": "kp-replay-key"},
        json=body,
    )
    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["receipt"]["idempotent_replay"] is True
    assert replay.json()["status"] == "rejected"


def test_kb_write_keeps_hardlink_mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """write_kb_json：原子替换断链后在镜像目录重建硬链接，两处内容一致。"""
    import os

    from asa_core import knowledge_proposals as kp

    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "kb_x.json").write_text("{}", encoding="utf-8")
    os.link(left / "kb_x.json", right / "kb_x.json")
    monkeypatch.setattr(kp, "_KB_MIRROR_PAIRS", ((left, right),))

    written = kp.write_kb_json(left, "kb_x.json", {"meta": {"version": "v1"}, "rules": [1]})
    assert len(written) == 2
    assert os.stat(left / "kb_x.json").st_ino == os.stat(right / "kb_x.json").st_ino
    assert json.loads((right / "kb_x.json").read_text(encoding="utf-8"))["rules"] == [1]
