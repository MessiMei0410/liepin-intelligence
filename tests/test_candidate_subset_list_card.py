"""POST /api/v1/candidates/list-card 子集名单卡端点回归守护。

背景（2026-08-19）：凡 Agent 给名单必须给可操作名单卡，禁止纯 markdown 表格名单。
整池筛选已有 candidate-list/refresh（asa_pool_filter），但精读/评审/去重等
"指定一组候选人"的子集名单（例：精读 20 人后"✅ 通过 4 人"）此前只能给静态表格。
本端点把一组 job_candidates id 组装成与整池卡同 schema 的 candidate_list 卡。

覆盖：
1. 单组（无 groups 参数）——全部进 subset 组，顺序保持入参顺序。
2. 多组分组——按 groups 组装，未覆盖的 id 自动归入「未分组」。
3. skipped——库中不存在的 id 在 summary.skipped 注明，不报错。
4. 空 candidate_ids → 409。
5. 卡片 schema 与既有整池 candidate_list 卡一致（type/title/context/summary/
   groups[{key,label,priority,candidates[{id,name,company,title,stage,flow_bucket}]}]）。
6. 只读语义——调用后库内数据不变。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from _local import env_path, fixture_base_db, require_local

import pytest
from fastapi.testclient import TestClient

from asa_core.app import create_app

SOURCE_DB = env_path("ASA_SOURCE_DB", Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db"))

# 高位测试 ID 段：避免与库内真实数据冲突。
CLIENT_ID = 980000001
JOB_ID = 980000001
JC_IDS = [980000001, 980000002, 980000003, 980000004]


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> dict:
    target = tmp_path_factory.mktemp("subset-list-card") / "asa.db"
    require_local(SOURCE_DB, "正式库 talent_system_v3")
    source = sqlite3.connect(fixture_base_db())
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    conn = sqlite3.connect(target)
    try:
        conn.execute("INSERT INTO clients (id, name) VALUES (?, '子集卡测试客户')", (CLIENT_ID,))
        conn.execute(
            "INSERT INTO jobs (id, client_id, title, status) VALUES (?, ?, '子集卡测试岗位', '已发布')",
            (JOB_ID, CLIENT_ID),
        )
        conn.executemany(
            "INSERT INTO people (id, display_name, current_company, current_title, fingerprint) VALUES (?,?,?,?,?)",
            [
                (JC_IDS[0], '子集张航', 'ASM中国集团公司', '高级机械设计工程师', 'subset-fp-1'),
                (JC_IDS[1], '子集陈**', '先导科技集团有限公司', '结构设计工程师', 'subset-fp-2'),
                (JC_IDS[2], '子集王先生', '华为', '机械技术专家', 'subset-fp-3'),
                (JC_IDS[3], '子集刘先生', '上海泽丰半导体科技有限公司', '机械工程师', 'subset-fp-4'),
            ],
        )
        conn.executemany(
            "INSERT INTO job_candidates (id, job_id, person_id, clean_stage, flow_bucket) VALUES (?,?,?,?,?)",
            [
                (JC_IDS[0], JOB_ID, JC_IDS[0], '已触达', '猎聘触达'),
                (JC_IDS[1], JOB_ID, JC_IDS[1], 'S1 新增寻访/待复核', '待复核'),
                (JC_IDS[2], JOB_ID, JC_IDS[2], 'S1 新增寻访/待复核', '待复核'),
                (JC_IDS[3], JOB_ID, JC_IDS[3], 'H5 最近寻访/初筛不通过', '最近寻访'),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return {"db_path": target}


def _post(client: TestClient, payload: dict) -> dict:
    response = client.post("/api/v1/candidates/list-card", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_subset_card_single_group_keeps_input_order(env: dict) -> None:
    with TestClient(create_app(db_path=env["db_path"], start_legacy=False)) as client:
        body = _post(client, {
            "candidate_ids": [JC_IDS[2], JC_IDS[0], JC_IDS[1]],
            "title": "长越机械｜精读通过名单",
            "context": {"type": "job", "id": JOB_ID},
        })
    assert body["ok"] is True
    assert isinstance(body["answer"], str) and "精读通过名单" in body["answer"]
    card = body["card"]
    assert card["type"] == "candidate_list"
    assert card["title"] == "长越机械｜精读通过名单"
    assert card["context"] == {"type": "job", "id": JOB_ID}
    assert card["subset"] is True
    assert card["summary"]["total"] == 3
    assert card["summary"]["skipped"] == []
    assert len(card["groups"]) == 1
    group = card["groups"][0]
    assert group["key"] == "subset"
    assert group["priority"] is False
    assert [c["id"] for c in group["candidates"]] == [JC_IDS[2], JC_IDS[0], JC_IDS[1]]


def test_subset_card_multi_groups_with_ungrouped_leftover(env: dict) -> None:
    with TestClient(create_app(db_path=env["db_path"], start_legacy=False)) as client:
        body = _post(client, {
            "candidate_ids": JC_IDS,
            "title": "精读结果",
            "groups": [
                {"key": "passed", "label": "✅ 通过", "candidate_ids": [JC_IDS[0], JC_IDS[2]], "priority": True},
                {"key": "rejected", "label": "❌ 不通过", "candidate_ids": [JC_IDS[3]]},
            ],
        })
    card = body["card"]
    assert [g["key"] for g in card["groups"]] == ["passed", "rejected", "ungrouped"]
    passed, rejected, ungrouped = card["groups"]
    assert passed["priority"] is True
    assert [c["id"] for c in passed["candidates"]] == [JC_IDS[0], JC_IDS[2]]
    assert [c["id"] for c in rejected["candidates"]] == [JC_IDS[3]]
    assert [c["id"] for c in ungrouped["candidates"]] == [JC_IDS[1]]
    # active/stopped 统计与整池卡同口径（clean_stage 停止词）。
    assert card["summary"]["total"] == 4
    assert card["summary"]["stopped"] == 1
    assert card["summary"]["active"] == 3


def test_subset_card_skipped_ids_noted_in_summary(env: dict) -> None:
    with TestClient(create_app(db_path=env["db_path"], start_legacy=False)) as client:
        body = _post(client, {
            "candidate_ids": [JC_IDS[0], 999999001, 999999002],
            "title": "子集名单",
        })
    card = body["card"]
    assert card["context"] is None
    assert card["summary"]["total"] == 1
    assert card["summary"]["requested"] == 3
    assert card["summary"]["skipped"] == [999999001, 999999002]
    assert [c["id"] for g in card["groups"] for c in g["candidates"]] == [JC_IDS[0]]
    assert "999999001" in body["answer"]


def test_subset_card_empty_candidate_ids_409(env: dict) -> None:
    with TestClient(create_app(db_path=env["db_path"], start_legacy=False)) as client:
        response = client.post("/api/v1/candidates/list-card", json={
            "candidate_ids": [], "title": "空名单",
        })
        assert response.status_code == 409
        assert "candidate_ids" in response.json()["detail"]
        # 非正整数 id 同样 409。
        response = client.post("/api/v1/candidates/list-card", json={
            "candidate_ids": [-3], "title": "非法 ID",
        })
        assert response.status_code == 409


def test_subset_card_schema_matches_pool_card(env: dict) -> None:
    """子集卡与整池 refresh 卡同 schema：顶层/分组/候选人字段集合一致。"""
    with TestClient(create_app(db_path=env["db_path"], start_legacy=False)) as client:
        pool = client.post(f"/api/v1/jobs/{JOB_ID}/candidate-list/refresh", json={}).json()["card"]
        subset = _post(client, {
            "candidate_ids": [JC_IDS[0], JC_IDS[1]], "title": "子集名单",
            "context": {"type": "job", "id": JOB_ID},
        })["card"]
    assert set(subset) >= set(pool), "子集卡必须包含整池卡全部顶层字段"
    pool_group_keys = {key for g in pool["groups"] for key in g}
    subset_group_keys = {key for g in subset["groups"] for key in g}
    assert subset_group_keys == pool_group_keys
    pool_candidate_keys = {key for g in pool["groups"] for c in g["candidates"] for key in c}
    subset_candidate_keys = {key for g in subset["groups"] for c in g["candidates"] for key in c}
    assert subset_candidate_keys == pool_candidate_keys == {"id", "name", "company", "title", "stage", "flow_bucket"}
    assert subset["type"] == pool["type"] == "candidate_list"
    assert subset["context"] == pool["context"]


def test_subset_card_is_readonly(env: dict) -> None:
    def snapshot() -> list[tuple]:
        conn = sqlite3.connect(env["db_path"])
        try:
            return conn.execute(
                "SELECT * FROM job_candidates WHERE job_id=? ORDER BY id", (JOB_ID,)
            ).fetchall()
        finally:
            conn.close()

    before = snapshot()
    with TestClient(create_app(db_path=env["db_path"], start_legacy=False)) as client:
        _post(client, {"candidate_ids": [JC_IDS[0], JC_IDS[1]], "title": "只读校验"})
    assert snapshot() == before
