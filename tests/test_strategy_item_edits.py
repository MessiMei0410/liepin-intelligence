"""寻访策略按项编辑（POST /api/v1/workflows/{id}/strategy/edits）回归守护。

覆盖：
1. 纯函数 apply_item_edits——关键词组/公司池/职级/顾问约束的增改删、目标项缺失抛错、
   删空公司池整层移除、accepted_levels 同步 evaluation_constraints。
2. 质量门 validate_edited_strategy——公司词两两成对、单词条 >2 词、组数/词条封顶、
   电源岗位裸公司词，违规则拒绝并说明。
3. 端点（TestClient + tmp_path 隔离库）——happy path 落新 revision artifact 并回读、
   幂等重放返回首次响应、外部寻访已开始 409、状态漂移（目标项不存在）409、
   工作流不存在 404、waiting_approval 旧审批卡作废并换新（不绕过 R3）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from _local import env_path, fixture_base_db, require_local

import pytest
from fastapi.testclient import TestClient

from asa_core.app import create_app
from a_system_agent import query_builders
from a_system_agent.strategy_editor import (
    MAX_KEYWORD_GROUPS,
    apply_item_edits,
    validate_edited_strategy,
)


SOURCE_DB = env_path("ASA_SOURCE_DB", Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db"))


@pytest.fixture(scope="module")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # 模块级共享副本：整模块只复制一次生产库（1.5GB）。client fixture 每次
    # _seed_workflow 前先删除固定主键的种子行，保证每个测试起点一致。
    target = tmp_path_factory.mktemp("strategy-item-edits") / "asa.db"
    require_local(SOURCE_DB, "正式库 talent_system_v3")
    source = sqlite3.connect(fixture_base_db())
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def _strategy_v2() -> dict:
    return {
        "schema_version": "strategy_v2",
        "input_level": "L2",
        "step1_job_essence": {"statement": "围绕电源工程师分层寻访", "value_chain_role": "电源工程师", "confirmed_by": "consultant"},
        "step2_target_pool": [
            {"path": "same_layer", "tier": "T1", "rationale": "T1 竞对", "companies": [
                {"name": "日立", "source": "client_doc", "confidence": "high"},
                {"name": "台达", "source": "client_doc", "confidence": "medium"},
            ]},
            {"path": "reverse", "tier": "T2", "rationale": "T2 客户整机厂", "companies": [
                {"name": "联想", "source": "kb_profile", "confidence": "high"},
            ]},
        ],
        "step3_level_mapping": {"accepted_levels": ["P5", "P6"], "calibration_rule": "按职责定档"},
        "evaluation_constraints": {"locations": [], "levels": ["P5", "P6"], "scenarios": []},
        "step4_keyword_groups": [
            {"group": "core_power", "targets": "T1 友商电源工程师", "terms": ["服务器电源", "电源模块"]},
            {"group": "scene", "targets": "整机厂场景", "terms": ["储能电源"]},
        ],
        "step5_expectation": {"expected_recall_per_tier": {"T1": 5, "T2": 8}, "fallback_plan": "放宽相邻池"},
        "negative_rules": [],
        "consultant_edits": [],
    }


def _seed_workflow(db_path: Path, *, workflow_status: str = "planned", sourcing_status: str = "pending") -> None:
    v2 = _strategy_v2()
    query_plan = query_builders.compile_query_plan_v1(v2)
    plan = {
        "channels": {
            "liepin": [{"query": "服务器电源", "purpose": "核心画像"}],
            "xsaas": [{"query": "日立", "purpose": "T1 公司"}],
        },
    }
    conn = sqlite3.connect(db_path)
    try:
        # 共享副本：先删除固定主键的种子工作流（含其审批/工件/步骤），再重建，
        # 避免 UNIQUE 冲突与前一测试的状态残留。
        conn.execute("DELETE FROM agent_approvals WHERE workflow_id='workflow_edit1'")
        conn.execute("DELETE FROM agent_artifacts WHERE workflow_id='workflow_edit1'")
        conn.execute("DELETE FROM agent_workflow_steps WHERE workflow_id='workflow_edit1'")
        conn.execute("DELETE FROM agent_workflows WHERE workflow_id='workflow_edit1'")
        conn.execute("DELETE FROM agent_goals WHERE goal_id='goal_edit1'")
        conn.execute(
            "INSERT INTO agent_goals(goal_id,objective,title,context_type,context_id,status) "
            "VALUES ('goal_edit1','为士兰微寻 10 位电源工程师','士兰微电源寻访','job',10,'planned')"
        )
        conn.execute(
            "INSERT INTO agent_workflows(workflow_id,goal_id,status,plan_json) "
            "VALUES ('workflow_edit1','goal_edit1',?,'{}')",
            (workflow_status,),
        )
        cursor = conn.execute(
            "INSERT INTO agent_workflow_steps(workflow_id,step_key,sequence,capability_id,business_label,business_stage,risk_level,status,output_json) "
            "VALUES ('workflow_edit1','s1',1,'search_strategy','生成多渠道寻访策略','search_strategy','R1','completed',?)",
            (json.dumps({"strategy": plan, "strategy_v2": v2, "query_plan_v1": query_plan}, ensure_ascii=False),),
        )
        strategy_step_id = int(cursor.lastrowid)
        cursor = conn.execute(
            "INSERT INTO agent_workflow_steps(workflow_id,step_key,sequence,capability_id,business_label,business_stage,risk_level,status) "
            "VALUES ('workflow_edit1','s2',2,'multi_channel_sourcing','执行多渠道寻访','sourcing','R3',?)",
            (sourcing_status,),
        )
        sourcing_step_id = int(cursor.lastrowid)
        metadata = {"plan": plan, "strategy_v2": v2, "query_plan_v1": query_plan, "schema_version": "strategy_v2"}
        conn.execute(
            "INSERT INTO agent_artifacts(artifact_id,goal_id,workflow_id,step_id,artifact_type,title,metadata_json,validation_status) "
            "VALUES ('artifact_edit1','goal_edit1','workflow_edit1',?,'search_strategy','多渠道寻访策略',?,'passed')",
            (strategy_step_id, json.dumps(metadata, ensure_ascii=False)),
        )
        if sourcing_status == "waiting_approval":
            conn.execute(
                "INSERT INTO agent_approvals(approval_id,goal_id,workflow_id,step_id,action_type,risk_level,title,preflight_json,status) "
                "VALUES ('approval_edit1','goal_edit1','workflow_edit1',?,'multi_channel_sourcing','R3','执行多渠道寻访','{}','pending')",
                (sourcing_step_id,),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def client(db_path: Path):
    _seed_workflow(db_path)
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as test_client:
        yield test_client


def _post_edits(client: TestClient, edits: list[dict], *, key: str, note: str = ""):
    preflight = client.post(
        "/api/v1/workflows/workflow_edit1/strategy/edits/preflight",
        json={"request_id": f"preflight-{key}", "edits": edits, "note": note},
    )
    if preflight.status_code != 200:
        return preflight
    preview = preflight.json()
    return client.post(
        "/api/v1/workflows/workflow_edit1/strategy/edits",
        headers={"Idempotency-Key": key},
        json={
            "request_id": f"req-{key}", "edits": edits, "note": note,
            "expected_strategy_hash": preview["strategy_hash"],
            "preflight_token": preview["preflight_token"],
        },
    )


# ---------------------------------------------------------------------------
# 纯函数：apply_item_edits
# ---------------------------------------------------------------------------


def test_apply_item_edits_keyword_group_update_and_delete() -> None:
    v2, applied = apply_item_edits(_strategy_v2(), [
        {"op": "update_keyword_group", "group": "core_power", "terms": ["通信电源"], "targets": "基站电源方向"},
        {"op": "delete_keyword_group", "group": "scene"},
    ])
    groups = {group["group"]: group for group in v2["step4_keyword_groups"]}
    assert list(groups) == ["core_power"]
    assert groups["core_power"]["terms"] == ["通信电源"]
    assert groups["core_power"]["targets"] == "基站电源方向"
    assert len(applied) == 2


def test_apply_item_edits_append_terms_and_negative_rule_without_overwriting() -> None:
    v2, applied = apply_item_edits(_strategy_v2(), [
        {"op": "append_keyword_terms", "group": "core_power", "terms": ["通信电源"]},
        {"op": "append_keyword_terms", "group": "顾问场景确认", "terms": ["AI 服务器"], "targets": "顾问确认场景"},
        {"op": "add_negative_rule", "type": "consultant_exclusion", "rule": "排除纯销售背景"},
    ])
    groups = {group["group"]: group for group in v2["step4_keyword_groups"]}
    assert groups["core_power"]["terms"] == ["服务器电源", "电源模块", "通信电源"]
    assert groups["顾问场景确认"]["terms"] == ["AI 服务器"]
    assert v2["negative_rules"][-1] == {
        "type": "consultant_exclusion", "rule": "排除纯销售背景",
        "source": "consultant_item_edit", "confidence": "high",
    }
    assert len(applied) == 3

    with pytest.raises(ValueError, match="已包含"):
        apply_item_edits(v2, [{"op": "append_keyword_terms", "group": "core_power", "terms": ["通信电源"]}])
    with pytest.raises(ValueError, match="已存在"):
        apply_item_edits(v2, [{"op": "add_negative_rule", "rule": "排除纯销售背景"}])


def test_apply_item_edits_add_group_rejects_duplicate_and_missing_target() -> None:
    v2, _ = apply_item_edits(_strategy_v2(), [
        {"op": "add_keyword_group", "group": "new_group", "terms": ["充电桩"], "targets": "新场景"},
    ])
    assert v2["step4_keyword_groups"][-1]["group"] == "new_group"
    with pytest.raises(ValueError, match="已存在"):
        apply_item_edits(_strategy_v2(), [{"op": "add_keyword_group", "group": "scene", "terms": ["x"]}])
    with pytest.raises(ValueError, match="不存在"):
        apply_item_edits(_strategy_v2(), [{"op": "update_keyword_group", "group": "ghost", "terms": ["x"]}])
    with pytest.raises(ValueError, match="不存在"):
        apply_item_edits(_strategy_v2(), [{"op": "delete_keyword_group", "group": "ghost"}])


def test_apply_item_edits_company_ops_by_tier() -> None:
    v2, applied = apply_item_edits(_strategy_v2(), [
        {"op": "delete_company", "tier": "T1", "name": "台达"},
        {"op": "update_company", "tier": "T1", "name": "日立", "confidence": "medium"},
        {"op": "add_company", "tier": "T3", "name": "维谛", "source": "legacy_profile_suggestions"},
    ])
    pools = {pool["tier"]: pool for pool in v2["step2_target_pool"]}
    assert [c["name"] for c in pools["T1"]["companies"]] == ["日立"]
    assert pools["T1"]["companies"][0]["confidence"] == "medium"
    assert pools["T3"]["companies"][0]["name"] == "维谛"
    assert pools["T3"]["companies"][0]["source"] == "legacy_profile_suggestions"
    assert len(applied) == 3
    with pytest.raises(ValueError, match="不在 T2 池内"):
        apply_item_edits(_strategy_v2(), [{"op": "delete_company", "tier": "T2", "name": "台达"}])


def test_apply_item_edits_delete_last_company_drops_pool_layer() -> None:
    v2, _ = apply_item_edits(_strategy_v2(), [{"op": "delete_company", "tier": "T2", "name": "联想"}])
    assert [pool["tier"] for pool in v2["step2_target_pool"]] == ["T1"]
    ok, errors = validate_edited_strategy(v2), None
    assert ok == []  # 单层池仍合法（companies 非空约束按层校验）


def test_apply_item_edits_levels_sync_evaluation_constraints() -> None:
    v2, applied = apply_item_edits(_strategy_v2(), [
        {"op": "update_accepted_levels", "accepted_levels": ["P6", "P7"]},
    ])
    assert v2["step3_level_mapping"]["accepted_levels"] == ["P6", "P7"]
    assert v2["evaluation_constraints"]["levels"] == ["P6", "P7"]
    assert "职级" in applied[0]["summary"]
    with pytest.raises(ValueError, match="不能为空"):
        apply_item_edits(_strategy_v2(), [{"op": "update_accepted_levels", "accepted_levels": []}])


def test_apply_item_edits_refreshes_consultant_judgement() -> None:
    original = _strategy_v2()
    original.update({
        "anchors": {
            "product_tech_line": {"present": True, "values": ["服务器电源"]},
            "scenario_track": {"present": True, "values": ["服务器"]},
        },
        "missing_anchors": [],
        "consultant_judgement": {
            "version": "senior_consultant_v1",
            "basis": ["岗位事实", "客户画像"],
            "role_diagnosis": {
                "role_family": "研发/工程",
                "business_mandate": "旧岗位判断",
                "core_differentiator": "旧核心判断",
                "candidate_archetype": "旧原型；职级参考 P5、P6",
            },
            "evidence_standard": {"direct_evidence": ["旧直接证据"], "must_verify": ["旧核验项"]},
            "client_calibration": {"must_confirm": ["旧客户校准项"]},
            "learning_application": {"positive_signals": ["旧正向反馈"]},
        },
    })
    v2, _ = apply_item_edits(original, [
        {"op": "delete_company", "tier": "T2", "name": "联想"},
        {"op": "update_accepted_levels", "accepted_levels": ["P6", "P7"]},
    ])
    judgement = v2["consultant_judgement"]
    assert judgement["role_diagnosis"]["role_family"] == "研发/工程"
    assert "P6、P7" in judgement["role_diagnosis"]["candidate_archetype"]
    assert judgement["market_view"]["transfer_paths_available"] == []
    assert judgement["market_view"]["same_layer_company_count"] == 2
    assert judgement["client_calibration"]["must_confirm"] == ["旧客户校准项"]
    assert judgement["learning_application"]["positive_signals"] == ["旧正向反馈"]


def test_apply_item_edits_backfills_consultant_judgement_for_legacy_strategy() -> None:
    original = _strategy_v2()
    assert "consultant_judgement" not in original

    v2, _ = apply_item_edits(original, [
        {"op": "update_accepted_levels", "accepted_levels": ["P6", "P7"]},
    ])

    judgement = v2["consultant_judgement"]
    assert judgement["version"] == "senior_consultant_v1"
    assert judgement["role_diagnosis"]["role_family"] == "研发/工程"
    assert judgement["search_sequence"][0]["name"] == "核心同层"
    assert "P6、P7" in judgement["role_diagnosis"]["candidate_archetype"]
    assert judgement["client_calibration"]["must_confirm"]

    v2["consultant_judgement"]["role_diagnosis"]["role_family"] = "待核验岗位族"
    refreshed, _ = apply_item_edits(v2, [
        {"op": "update_accepted_levels", "accepted_levels": ["P6", "P7"]},
    ])
    assert refreshed["consultant_judgement"]["role_diagnosis"]["role_family"] == "研发/工程"


def test_apply_item_edits_consultant_constraints_and_unknown_op() -> None:
    v2, _ = apply_item_edits(_strategy_v2(), [
        {"op": "update_consultant_constraints", "constraints": [
            {"type": "hard_requirement", "rule": "必须有量产经验"},
            {"type": "bogus", "rule": "随便看看"},
            {"type": "preference", "rule": "  "},
        ]},
    ])
    constraints = v2["consultant_constraints"]
    assert [(item["type"], item["rule"]) for item in constraints] == [
        ("hard_requirement", "必须有量产经验"),
        ("consultant_wording", "随便看看"),  # 非法 type 降级 consultant_wording；空 rule 被过滤
    ]
    with pytest.raises(ValueError, match="op 非法"):
        apply_item_edits(_strategy_v2(), [{"op": "delete_everything"}])


# ---------------------------------------------------------------------------
# 质量门：validate_edited_strategy
# ---------------------------------------------------------------------------


def test_validate_rejects_company_token_pairing() -> None:
    v2, _ = apply_item_edits(_strategy_v2(), [
        {"op": "update_keyword_group", "group": "core_power", "terms": ["日立 台达"]},
    ])
    errors = validate_edited_strategy(v2)
    assert any("两两成对" in error for error in errors)


def test_validate_rejects_overlong_term_and_caps() -> None:
    v2, _ = apply_item_edits(_strategy_v2(), [
        {"op": "update_keyword_group", "group": "core_power", "terms": ["alpha beta gamma"]},
    ])
    assert any("超过 2 个词" in error for error in validate_edited_strategy(v2))

    many = _strategy_v2()
    many["step4_keyword_groups"] = [
        *many["step4_keyword_groups"],
        *[{"group": f"extra_{index}", "targets": "", "terms": [f"词{index}"]} for index in range(MAX_KEYWORD_GROUPS)],
    ]
    assert any("封顶" in error for error in validate_edited_strategy(many))


def test_validate_power_role_rejects_bare_company_term() -> None:
    v2 = _strategy_v2()
    v2["step4_keyword_groups"] = [
        {"group": "power_core", "targets": "VRM TLVR 电源专家", "terms": ["多相控制器", "VRM"]},
        {"group": "bare_company", "targets": "", "terms": ["日立"]},
    ]
    errors = validate_edited_strategy(v2)
    assert any("裸公司词" in error for error in errors)
    # 去掉裸公司词后电源岗位策略本身合法
    v2["step4_keyword_groups"] = v2["step4_keyword_groups"][:1]
    assert validate_edited_strategy(v2) == []


# ---------------------------------------------------------------------------
# 端点：revision / 幂等 / 状态闸门 / 审批语义
# ---------------------------------------------------------------------------


def test_strategy_item_edits_happy_path_creates_new_revision(client: TestClient, db_path: Path) -> None:
    response = _post_edits(client, [
        {"op": "update_keyword_group", "group": "core_power", "terms": ["通信电源", "基站电源"], "targets": "基站电源方向"},
        {"op": "delete_company", "tier": "T1", "name": "台达"},
        {"op": "update_accepted_levels", "accepted_levels": ["P6"]},
    ], key="se-happy-1", note="顾问逐条确认")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["revision"] == 1
    assert payload["edit_count"] == 3
    assert payload["strategy_hash"]
    assert payload["approval_refreshed"] is False

    # 结果回读：响应内 strategy_v2 即为落库版本
    v2 = payload["strategy_v2"]
    groups = {group["group"]: group for group in v2["step4_keyword_groups"]}
    assert groups["core_power"]["terms"] == ["通信电源", "基站电源"]
    assert v2["consultant_edits"][-1]["type"] == "item_edit"

    # 落库核验：新 artifact 为最新 search_strategy，旧 artifact 不被改写
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT artifact_id,metadata_json FROM agent_artifacts "
            "WHERE workflow_id='workflow_edit1' AND artifact_type='search_strategy' ORDER BY id"
        ).fetchall()
        step_output = conn.execute(
            "SELECT output_json FROM agent_workflow_steps WHERE workflow_id='workflow_edit1' AND capability_id='search_strategy'"
        ).fetchone()
    finally:
        conn.close()
    assert len(rows) == 2
    assert rows[0][0] == "artifact_edit1"
    latest = json.loads(rows[1][1])
    assert latest["edited_via"] == "strategy_item_edit"
    assert latest["strategy_v2"]["edit_revision"] == 1
    assert latest["strategy_v2"]["step3_level_mapping"]["accepted_levels"] == ["P6"]
    assert latest["query_plan_v1"]["schema_version"] == "query_plan_v1"
    output = json.loads(step_output[0])
    assert output["strategy_v2"]["edit_revision"] == 1


def test_strategy_item_edits_preflight_is_read_only_and_token_is_single_use(client: TestClient, db_path: Path) -> None:
    edits = [{"op": "append_keyword_terms", "group": "顾问对话确认", "terms": ["通信电源"]}]
    preflight = client.post(
        "/api/v1/workflows/workflow_edit1/strategy/edits/preflight",
        json={"request_id": "preflight-single", "edits": edits},
    )
    assert preflight.status_code == 200, preflight.text
    preview = preflight.json()
    assert preview["preflight_token"]
    assert preview["strategy_hash"]
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_artifacts WHERE workflow_id='workflow_edit1' AND artifact_type='search_strategy'"
        ).fetchone()[0] == 1
    finally:
        conn.close()

    body = {
        "request_id": "commit-single", "edits": edits,
        "expected_strategy_hash": preview["strategy_hash"], "preflight_token": preview["preflight_token"],
    }
    first = client.post(
        "/api/v1/workflows/workflow_edit1/strategy/edits",
        headers={"Idempotency-Key": "commit-single"}, json=body,
    )
    assert first.status_code == 200, first.text
    replay_with_new_key = client.post(
        "/api/v1/workflows/workflow_edit1/strategy/edits",
        headers={"Idempotency-Key": "commit-single-reuse"}, json={**body, "request_id": "commit-single-reuse"},
    )
    assert replay_with_new_key.status_code == 409
    assert "预检已失效" in replay_with_new_key.json()["detail"]


def test_strategy_item_edits_preflight_rejects_stale_strategy_hash(client: TestClient) -> None:
    response = client.post(
        "/api/v1/workflows/workflow_edit1/strategy/edits/preflight",
        json={
            "request_id": "preflight-stale", "expected_strategy_hash": "old_hash",
            "edits": [{"op": "delete_keyword_group", "group": "scene"}],
        },
    )
    assert response.status_code == 409
    assert "基于旧版本" in response.json()["detail"]


def test_strategy_item_edits_rechecks_sourcing_state_before_any_write(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_workflow(db_path)
    edits = [{"op": "delete_keyword_group", "group": "scene"}]
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        preflight = client.post(
            "/api/v1/workflows/workflow_edit1/strategy/edits/preflight",
            json={"request_id": "race-preflight", "edits": edits},
        )
        assert preflight.status_code == 200, preflight.text
        preview = preflight.json()

        original_compile = query_builders.compile_query_plan_v1
        state_changed = False

        def compile_after_sourcing_started(strategy: dict) -> dict:
            nonlocal state_changed
            result = original_compile(strategy)
            if not state_changed:
                state_changed = True
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute(
                        "UPDATE agent_workflows SET status='paused' WHERE workflow_id='workflow_edit1'"
                    )
                    conn.execute(
                        "UPDATE agent_workflow_steps SET status='waiting_external' "
                        "WHERE workflow_id='workflow_edit1' AND capability_id='multi_channel_sourcing'"
                    )
                    conn.commit()
                finally:
                    conn.close()
            return result

        monkeypatch.setattr(query_builders, "compile_query_plan_v1", compile_after_sourcing_started)
        response = client.post(
            "/api/v1/workflows/workflow_edit1/strategy/edits",
            headers={"Idempotency-Key": "race-commit"},
            json={
                "request_id": "race-commit", "edits": edits,
                "expected_strategy_hash": preview["strategy_hash"],
                "preflight_token": preview["preflight_token"],
            },
        )
    assert response.status_code == 409
    assert "外部寻访已经开始" in response.json()["detail"]

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_artifacts WHERE workflow_id='workflow_edit1' AND artifact_type='search_strategy'"
        ).fetchone()[0] == 1
        output = json.loads(conn.execute(
            "SELECT output_json FROM agent_workflow_steps WHERE workflow_id='workflow_edit1' AND capability_id='search_strategy'"
        ).fetchone()[0])
        assert output["strategy_v2"].get("edit_revision") is None
    finally:
        conn.close()


def test_strategy_item_edits_idempotent_replay(client: TestClient, db_path: Path) -> None:
    edits = [{"op": "delete_company", "tier": "T1", "name": "台达"}]
    preflight = client.post(
        "/api/v1/workflows/workflow_edit1/strategy/edits/preflight",
        json={"request_id": "preflight-se-idem-1", "edits": edits},
    ).json()
    body = {
        "request_id": "req-se-idem-1", "edits": edits,
        "expected_strategy_hash": preflight["strategy_hash"],
        "preflight_token": preflight["preflight_token"],
    }
    first = client.post(
        "/api/v1/workflows/workflow_edit1/strategy/edits",
        headers={"Idempotency-Key": "se-idem-1"}, json=body,
    )
    replay = client.post(
        "/api/v1/workflows/workflow_edit1/strategy/edits",
        headers={"Idempotency-Key": "se-idem-1"}, json=body,
    )
    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["receipt"]["idempotent_replay"] is True
    assert replay.json()["revision"] == first.json()["revision"]
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM agent_artifacts WHERE workflow_id='workflow_edit1' AND artifact_type='search_strategy'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 2  # 重放不产生第三个 artifact


def test_strategy_item_edits_conflict_when_target_missing(client: TestClient) -> None:
    response = _post_edits(client, [{"op": "update_keyword_group", "group": "ghost", "terms": ["x"]}], key="se-409-1")
    assert response.status_code == 409
    assert "不存在" in response.json()["detail"]


def test_strategy_item_edits_rejects_quality_violation(client: TestClient) -> None:
    response = _post_edits(client, [
        {"op": "update_keyword_group", "group": "core_power", "terms": ["日立 台达"]},
    ], key="se-409-quality")
    assert response.status_code == 409
    assert "公司词" in response.json()["detail"]


def test_strategy_item_edits_rejects_after_sourcing_started(db_path: Path) -> None:
    # 注意：WorkflowEngine 启动恢复会把 running 步骤/工作流改写成 pending/paused，
    # 因此用 waiting_external（不受恢复改写）模拟“外部寻访已开始”。
    _seed_workflow(db_path, workflow_status="paused", sourcing_status="waiting_external")
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = _post_edits(client, [{"op": "delete_keyword_group", "group": "scene"}], key="se-409-started")
    assert response.status_code == 409
    assert "外部寻访已经开始" in response.json()["detail"]


def test_strategy_item_edits_workflow_not_found(db_path: Path) -> None:
    _seed_workflow(db_path)
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = client.post(
            "/api/v1/workflows/workflow_ghost/strategy/edits/preflight",
            json={"request_id": "req-se-404", "edits": [{"op": "delete_keyword_group", "group": "scene"}]},
        )
    assert response.status_code == 404


def test_strategy_item_edits_refreshes_pending_approval(db_path: Path) -> None:
    _seed_workflow(db_path, workflow_status="waiting_approval", sourcing_status="waiting_approval")
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = _post_edits(client, [{"op": "delete_keyword_group", "group": "scene"}], key="se-approval-1")
        assert response.status_code == 200, response.text
        assert response.json()["approval_refreshed"] is True
    conn = sqlite3.connect(db_path)
    try:
        approvals = conn.execute(
            "SELECT approval_id,status,preflight_json FROM agent_approvals WHERE workflow_id='workflow_edit1' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert len(approvals) == 2
    # 旧卡作废（带 approval_id 后缀避免 (step_id,status) 唯一约束冲突），新卡 pending 且携带新快照 hash
    assert approvals[0][1] == "superseded_strategy_edit_approval_edit1"
    assert approvals[1][1] == "pending"
    preflight = json.loads(approvals[1][2])
    assert preflight["strategy_hash"] == response.json()["strategy_hash"]
