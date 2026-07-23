"""S4-3c：复盘 diff 逐项采纳/拒绝的后端持久化测试。

口径：docs/ASA_PRD_S4_sourcing_strategy_agent_2026-07-23.md §2 硬性约束第 4 条。
全部使用临时库（AgentDbCase 基座 / 临时 sqlite），绝不触碰生产 DB。
覆盖：PATCH 200/404/409、execute_idempotent 幂等重放与同键异负载 409、逐项 status 落库
（upsert 可重复覆盖）、strategy_v2.consultant_edits 追加、explicit_corrections
（strategy_corrections 表）写入、strategy_v2 缺失降级不报错、restricted 字面量不回泄。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from a_system_agent import strategy_review  # noqa: E402
from asa_core.app import create_app  # noqa: E402
from test_strategy_review_s4 import (  # noqa: E402
    FORBIDDEN_LITERALS,
    KB_CASE_FIXTURE,
    ReviewDbCase,
    STRATEGY_V2_FIXTURE,
    _write_review_kb,
)


def _review_fixture(workflow_id: str) -> dict:
    """确定性复盘 fixture：3 条 pending 修订（step2 增列 / step4 替换 / step1 复核）。"""
    return {
        "schema_version": "strategy_review_v1",
        "generator": "rule_v1",
        "workflow_id": workflow_id,
        "goal_id": f"goal_{workflow_id}",
        "verdict": "strategy_too_narrow",
        "verdict_label": "策略问题：关键词/目标池太窄",
        "verdict_reason": "本轮总召回 10 < step5 预期总量 40 的 50%，判定策略问题",
        "degraded": False,
        "thresholds": {},
        "evidence": {},
        "per_channel_findings": [],
        "revision_diff": [
            {
                "diff_id": "diff-1", "step": "step2_target_pool", "op": "add", "tier": "T2",
                "companies": ["下游X公司", "下游Y公司"],
                "reason": "召回 10 不及预期 50%（40），按 fallback_plan 放宽目标池：增列 T2 公司",
                "status": "pending",
            },
            {
                "diff_id": "diff-2", "step": "step4_keyword_groups", "op": "replace", "group": "core",
                "terms": ["真空腔体", "传动机构"],
                "reason": "关键词组“core”召回不足，建议替换为更宽的知识库锚定词组",
                "status": "pending",
            },
            {
                "diff_id": "diff-3", "step": "step1_job_essence", "op": "review",
                "reason": "高分率偏低：建议复核岗位本质与 step3 定档口径",
                "status": "pending",
            },
        ],
        "escalation": None,
        "notes": [],
        "client": "长越科技",
        "job": "机械高级工程师",
        "generated_at": "2026-07-23 10:00:00",
    }


class DiffDecisionDbCase(ReviewDbCase):
    """公共 fixture：临时库 + 终局工作流 + strategy artifact + strategy_review artifact。"""

    def make_review(self, workflow_id: str = "wf-d1", *, with_strategy: bool = True, review: dict | None = None) -> str:
        self.make_terminal_workflow(workflow_id, created_at="2026-07-20 10:00:00")
        if with_strategy:
            self.insert_strategy_artifact(workflow_id)
        doc = review or _review_fixture(workflow_id)
        conn = sqlite3.connect(self.db_path)
        try:
            artifact_id = strategy_review.upsert_strategy_review(conn, doc)
            conn.commit()
        finally:
            conn.close()
        return artifact_id

    def strategy_metadata(self, workflow_id: str) -> dict:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT metadata_json FROM agent_artifacts WHERE workflow_id=? AND artifact_type='search_strategy' ORDER BY id DESC LIMIT 1",
                (workflow_id,),
            ).fetchone()
            return json.loads(row["metadata_json"]) if row else {}
        finally:
            conn.close()

    def corrections_row(self, client: str = "长越科技", position: str = "机械高级工程师") -> dict | None:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM strategy_corrections WHERE client=? AND position=? ORDER BY id DESC LIMIT 1",
                (client, position),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


class ApplyDiffDecisionsTest(DiffDecisionDbCase):
    """服务层：逐项 status 落库（upsert 覆盖）+ consultant_edits + explicit_corrections。"""

    def test_apply_updates_status_and_persists(self) -> None:
        artifact_id = self.make_review()
        result = self.service.apply_strategy_review_diff_decisions(
            "wf-d1",
            [
                {"diff_id": "diff-1", "status": "accepted"},
                {"diff_id": "diff-2", "status": "rejected"},
            ],
        )
        assert result["ok"] is True
        assert result["artifact_id"] == artifact_id
        assert result["updated"] == 2
        by_id = {item["diff_id"]: item for item in result["revision_diff"]}
        assert by_id["diff-1"]["status"] == "accepted"
        assert by_id["diff-2"]["status"] == "rejected"
        assert by_id["diff-3"]["status"] == "pending", "未决策条目保持 pending"
        assert by_id["diff-1"]["decided_at"] and by_id["diff-2"]["decided_at"]
        # 落库：artifact metadata 与 GET 读取均为最新决策
        stored = {item["diff_id"]: item for item in json.loads(self.review_row("wf-d1")["metadata_json"])["revision_diff"]}
        assert stored["diff-1"]["status"] == "accepted"
        assert stored["diff-2"]["status"] == "rejected"
        loaded = self.service.get_strategy_review("wf-d1")
        loaded_by_id = {item["diff_id"]: item for item in loaded["review"]["revision_diff"]}
        assert loaded_by_id["diff-1"]["status"] == "accepted"
        assert loaded_by_id["diff-2"]["status"] == "rejected"

    def test_apply_upsert_overwrite_keeps_single_consultant_edit(self) -> None:
        self.make_review()
        self.service.apply_strategy_review_diff_decisions("wf-d1", [{"diff_id": "diff-1", "status": "accepted"}])
        # 同一 diff 重复决策：覆盖而非重复追加（upsert 可重复覆盖）
        result = self.service.apply_strategy_review_diff_decisions("wf-d1", [{"diff_id": "diff-1", "status": "rejected"}])
        by_id = {item["diff_id"]: item for item in result["revision_diff"]}
        assert by_id["diff-1"]["status"] == "rejected"
        edits = self.strategy_metadata("wf-d1")["strategy_v2"]["consultant_edits"]
        matching = [item for item in edits if item["diff_id"] == "diff-1"]
        assert len(matching) == 1
        assert matching[0]["status"] == "rejected"

    def test_consultant_edits_appended_with_full_shape(self) -> None:
        self.make_review()
        result = self.service.apply_strategy_review_diff_decisions(
            "wf-d1",
            [{"diff_id": "diff-1", "status": "accepted"}, {"diff_id": "diff-2", "status": "rejected"}],
        )
        assert result["consultant_edits_appended"] == 2
        edits = self.strategy_metadata("wf-d1")["strategy_v2"]["consultant_edits"]
        assert len(edits) == 2
        first = next(item for item in edits if item["diff_id"] == "diff-1")
        assert first["step"] == "step2_target_pool" and first["op"] == "add"
        assert first["status"] == "accepted"
        assert first["reason"].strip() and first["decided_at"].strip()
        second = next(item for item in edits if item["diff_id"] == "diff-2")
        assert second["step"] == "step4_keyword_groups" and second["op"] == "replace"
        assert second["status"] == "rejected"

    def test_explicit_corrections_written(self) -> None:
        self.make_review()
        result = self.service.apply_strategy_review_diff_decisions(
            "wf-d1",
            [{"diff_id": "diff-1", "status": "accepted"}, {"diff_id": "diff-2", "status": "accepted"}],
        )
        assert result["learning_signal_recorded"] is True
        row = self.corrections_row()
        assert row, "explicit_corrections 落点 strategy_corrections 必须有该 client+position 行"
        assert json.loads(row["promote_keywords_json"]) == ["真空腔体", "传动机构"], "采纳 step4 词组 → promote"
        assert json.loads(row["target_tags_json"]) == ["下游X公司", "下游Y公司"], "采纳 step2 公司 → target"
        evidence = json.loads(row["evidence_json"])
        assert any("diff-1" in item and "采纳" in item for item in evidence)
        assert any("diff-2" in item for item in evidence)

    def test_explicit_corrections_flip_retracts_opposite_signal(self) -> None:
        self.make_review()
        self.service.apply_strategy_review_diff_decisions("wf-d1", [{"diff_id": "diff-2", "status": "accepted"}])
        # 顾问翻转为拒绝：词组应从 promote 撤回、进入 suppress
        self.service.apply_strategy_review_diff_decisions("wf-d1", [{"diff_id": "diff-2", "status": "rejected"}])
        row = self.corrections_row()
        assert json.loads(row["promote_keywords_json"]) == []
        assert json.loads(row["suppress_keywords_json"]) == ["真空腔体", "传动机构"]

    def test_validation_errors_and_atomic_batch(self) -> None:
        self.make_review()
        with self.assertRaises(ValueError):
            self.service.apply_strategy_review_diff_decisions("wf-d1", [{"diff_id": "diff-9", "status": "accepted"}])
        with self.assertRaises(ValueError):
            self.service.apply_strategy_review_diff_decisions("wf-d1", [{"diff_id": "diff-1", "status": "pending"}])
        # 整批先校验：任一非法则全部不写入
        with self.assertRaises(ValueError):
            self.service.apply_strategy_review_diff_decisions(
                "wf-d1",
                [{"diff_id": "diff-1", "status": "accepted"}, {"diff_id": "diff-x", "status": "accepted"}],
            )
        stored = {item["diff_id"]: item["status"] for item in json.loads(self.review_row("wf-d1")["metadata_json"])["revision_diff"]}
        assert stored == {"diff-1": "pending", "diff-2": "pending", "diff-3": "pending"}
        with self.assertRaises(LookupError):
            self.service.apply_strategy_review_diff_decisions("wf-missing", [{"diff_id": "diff-1", "status": "accepted"}])
        self.make_terminal_workflow("wf-noreview", created_at="2026-07-21 10:00:00")
        with self.assertRaises(LookupError):
            self.service.apply_strategy_review_diff_decisions("wf-noreview", [{"diff_id": "diff-1", "status": "accepted"}])

    def test_degrades_without_strategy_v2(self) -> None:
        # 无 search_strategy artifact：consultant_edits 跳过（appended=0），决策本身仍落库，学习信号照写
        artifact_id = self.make_review(with_strategy=False)
        result = self.service.apply_strategy_review_diff_decisions("wf-d1", [{"diff_id": "diff-2", "status": "accepted"}])
        assert result["ok"] is True and result["artifact_id"] == artifact_id
        assert result["consultant_edits_appended"] == 0
        assert result["learning_signal_recorded"] is True
        by_id = {item["diff_id"]: item for item in result["revision_diff"]}
        assert by_id["diff-2"]["status"] == "accepted"
        assert self.corrections_row() is not None

    def test_degrades_without_client_job_anchor(self) -> None:
        review = _review_fixture("wf-d1")
        review["client"] = ""
        review["job"] = ""
        self.make_review(review=review)
        result = self.service.apply_strategy_review_diff_decisions("wf-d1", [{"diff_id": "diff-1", "status": "accepted"}])
        assert result["ok"] is True
        assert result["consultant_edits_appended"] == 1, "strategy_v2 在库时 consultant_edits 不受锚点缺失影响"
        assert result["learning_signal_recorded"] is False, "缺 client/job 锚点不硬写学习信号，降级不报错"

    def test_decisions_do_not_echo_restricted_literals(self) -> None:
        self.make_review()
        result = self.service.apply_strategy_review_diff_decisions(
            "wf-d1",
            [{"diff_id": "diff-1", "status": "accepted"}, {"diff_id": "diff-2", "status": "rejected"}],
        )
        row = self.corrections_row()
        encoded = json.dumps(result, ensure_ascii=False) + json.dumps(row, ensure_ascii=False)
        # strategy_v2 文档本体合法持有 negative_rules（内部输入，不外泄）；只校验新增的 consultant_edits
        encoded += json.dumps(self.strategy_metadata("wf-d1")["strategy_v2"]["consultant_edits"], ensure_ascii=False)
        for literal in FORBIDDEN_LITERALS + ["青岛芯恩", "福建晋华"]:
            assert literal not in encoded, f"restricted 字面量不得出现在决策落库链路：{literal}"


class DiffDecisionsApiTest(unittest.TestCase):
    """API：PATCH /strategy-review/diffs 的 200/404/409 与 execute_idempotent 幂等语义。"""

    API_SCHEMA = """
    CREATE TABLE clients(id INTEGER PRIMARY KEY,name TEXT);
    CREATE TABLE jobs(id INTEGER PRIMARY KEY,client_id INTEGER,title TEXT);
    CREATE TABLE positions(id INTEGER PRIMARY KEY,client TEXT,title TEXT);
    CREATE TABLE people(id INTEGER PRIMARY KEY,display_name TEXT,current_company TEXT);
    CREATE TABLE candidates(id INTEGER PRIMARY KEY,name TEXT,company TEXT,title TEXT,education TEXT,
      experience TEXT,skills TEXT,city TEXT,client TEXT,position TEXT,source TEXT,xsaas_id TEXT,
      search_date TEXT,status TEXT,notes TEXT,updated_at TEXT);
    CREATE TABLE job_candidates(id INTEGER PRIMARY KEY,job_id INTEGER,person_id INTEGER,raw_client TEXT,
      raw_position TEXT,raw_status TEXT,raw_stage TEXT,clean_stage TEXT,flow_bucket TEXT,updated_at TEXT,
      source_candidate_id TEXT);
    CREATE TABLE candidate_events(id INTEGER PRIMARY KEY,job_candidate_id INTEGER,person_id INTEGER,job_id INTEGER,
      event_type TEXT,event_status TEXT,event_time TEXT,summary TEXT,raw_json TEXT,source_table TEXT,source_id TEXT);
    CREATE TABLE source_profiles(id INTEGER PRIMARY KEY,person_id INTEGER,source_type TEXT,
      source_candidate_id TEXT,source_date TEXT,raw_status TEXT,raw_client TEXT,raw_position TEXT,raw_json TEXT);
    """

    def setUp(self) -> None:
        self.kb_temp = tempfile.TemporaryDirectory()
        kb_dir = _write_review_kb(Path(self.kb_temp.name) / "kb", archetype=None, case=KB_CASE_FIXTURE)
        self._old_kb_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(kb_dir)
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "asa.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(self.API_SCHEMA)
        conn.execute("INSERT INTO clients VALUES (1,'长越科技')")
        conn.execute("INSERT INTO jobs VALUES (10,1,'机械高级工程师')")
        conn.commit()
        conn.close()
        self.app = create_app(db_path=self.db_path, start_legacy=False)
        self._seed()

    def tearDown(self) -> None:
        if self._old_kb_env is None:
            os.environ.pop("ASA_KNOWLEDGE_BASE_DIR", None)
        else:
            os.environ["ASA_KNOWLEDGE_BASE_DIR"] = self._old_kb_env
        self.kb_temp.cleanup()
        self.temp.cleanup()

    def _seed(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO agent_goals (goal_id,objective,title,context_type,context_id,context_json,status,business_outcome)
            VALUES ('goal_wf-api','给长越科技机械高级工程师补充10位合适人选','寻访','job',10,'{"type":"job","id":10}','blocked','completed_pool_insufficient')
            """
        )
        conn.execute(
            "INSERT INTO agent_workflows (workflow_id,goal_id,status,business_outcome) VALUES ('wf-api','goal_wf-api','blocked','completed_pool_insufficient')"
        )
        conn.execute(
            """
            INSERT INTO agent_artifacts
            (artifact_id,goal_id,workflow_id,step_id,artifact_type,title,mime_type,content,metadata_json,validation_status)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "artifact_strategy_wf-api", "goal_wf-api", "wf-api", None,
                "search_strategy", "多渠道寻访策略", "text/markdown", "# 策略",
                json.dumps({"strategy_v2": STRATEGY_V2_FIXTURE}, ensure_ascii=False), "passed",
            ),
        )
        conn.execute(
            "INSERT INTO agent_goals (goal_id,objective,title,context_type,context_id,context_json,status,business_outcome) VALUES ('goal_noreview','寻访','寻访','job',10,'{\"type\":\"job\",\"id\":10}','blocked','completed_pool_insufficient')"
        )
        conn.execute(
            "INSERT INTO agent_workflows (workflow_id,goal_id,status,business_outcome) VALUES ('wf-noreview','goal_noreview','blocked','completed_pool_insufficient')"
        )
        conn.commit()
        artifact_id = strategy_review.upsert_strategy_review(conn, _review_fixture("wf-api"))
        assert artifact_id == "strategy_review_wf-api"
        conn.commit()
        conn.close()

    def _corrections(self) -> dict | None:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM strategy_corrections WHERE client='长越科技' AND position='机械高级工程师'"
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def test_patch_200_replay_and_conflict(self) -> None:
        with TestClient(self.app) as client:
            headers = {"Idempotency-Key": "diffs-key-1"}
            body = {
                "request_id": "req-diffs-1",
                "decisions": [
                    {"diff_id": "diff-1", "status": "accepted"},
                    {"diff_id": "diff-2", "status": "rejected"},
                ],
            }
            first = client.patch("/api/v1/workflows/wf-api/strategy-review/diffs", json=body, headers=headers)
            assert first.status_code == 200, first.text
            payload = first.json()
            assert payload["ok"] is True
            assert payload["workflow_id"] == "wf-api"
            assert payload["artifact_id"] == "strategy_review_wf-api"
            assert payload["updated"] == 2
            by_id = {item["diff_id"]: item for item in payload["revision_diff"]}
            assert by_id["diff-1"]["status"] == "accepted"
            assert by_id["diff-2"]["status"] == "rejected"
            assert by_id["diff-3"]["status"] == "pending"
            assert payload["receipt"]["idempotent_replay"] is False

            # 幂等重放：同键同负载返回首次结果，不重复写（consultant_edits 不翻倍）
            replay = client.patch("/api/v1/workflows/wf-api/strategy-review/diffs", json=body, headers=headers)
            assert replay.status_code == 200
            replayed = replay.json()
            assert replayed["receipt"]["idempotent_replay"] is True
            assert replayed["updated"] == 2
            conn = sqlite3.connect(self.db_path)
            metadata = json.loads(
                conn.execute(
                    "SELECT metadata_json FROM agent_artifacts WHERE artifact_id='artifact_strategy_wf-api'"
                ).fetchone()[0]
            )
            conn.close()
            assert len(metadata["strategy_v2"]["consultant_edits"]) == 2, "重放不得重复追加 consultant_edits"

            # 同键不同负载 → 409
            conflict = client.patch(
                "/api/v1/workflows/wf-api/strategy-review/diffs",
                json={"request_id": "req-diffs-2", "decisions": [{"diff_id": "diff-3", "status": "accepted"}]},
                headers=headers,
            )
            assert conflict.status_code == 409, "同 Idempotency-Key 不同负载 → 409"

            # 决策落库后 GET strategy-review 事实源可见
            got = client.get("/api/v1/workflows/wf-api/strategy-review")
            assert got.status_code == 200
            got_by_id = {item["diff_id"]: item for item in got.json()["review"]["revision_diff"]}
            assert got_by_id["diff-1"]["status"] == "accepted"
            assert got_by_id["diff-2"]["status"] == "rejected"

            # explicit_corrections 落点
            row = self._corrections()
            assert row is not None
            assert json.loads(row["target_tags_json"]) == ["下游X公司", "下游Y公司"]
            assert json.loads(row["suppress_keywords_json"]) == ["真空腔体", "传动机构"]

    def test_patch_404_and_409_semantics(self) -> None:
        with TestClient(self.app) as client:
            missing = client.patch(
                "/api/v1/workflows/wf-missing/strategy-review/diffs",
                json={"request_id": "req-404-1", "decisions": [{"diff_id": "diff-1", "status": "accepted"}]},
                headers={"Idempotency-Key": "diffs-404-1"},
            )
            assert missing.status_code == 404, "工作流不存在 → 404"
            none_yet = client.patch(
                "/api/v1/workflows/wf-noreview/strategy-review/diffs",
                json={"request_id": "req-404-2", "decisions": [{"diff_id": "diff-1", "status": "accepted"}]},
                headers={"Idempotency-Key": "diffs-404-2"},
            )
            assert none_yet.status_code == 404, "工作流存在但无复盘 → 404"

            unknown = client.patch(
                "/api/v1/workflows/wf-api/strategy-review/diffs",
                json={"request_id": "req-409-1", "decisions": [{"diff_id": "diff-x", "status": "accepted"}]},
                headers={"Idempotency-Key": "diffs-409-1"},
            )
            assert unknown.status_code == 409, "diff_id 不存在 → 409"
            illegal = client.patch(
                "/api/v1/workflows/wf-api/strategy-review/diffs",
                json={"request_id": "req-409-2", "decisions": [{"diff_id": "diff-1", "status": "maybe"}]},
                headers={"Idempotency-Key": "diffs-409-2"},
            )
            assert illegal.status_code == 409, "非法状态 → 409"

    def test_patch_output_never_echoes_restricted(self) -> None:
        with TestClient(self.app) as client:
            response = client.patch(
                "/api/v1/workflows/wf-api/strategy-review/diffs",
                json={"request_id": "req-sec-1", "decisions": [{"diff_id": "diff-1", "status": "accepted"}]},
                headers={"Idempotency-Key": "diffs-sec-1"},
            )
            assert response.status_code == 200
            encoded = json.dumps(response.json(), ensure_ascii=False)
            row = self._corrections()
            assert row is not None
            encoded += json.dumps(row, ensure_ascii=False)
            for literal in FORBIDDEN_LITERALS + ["青岛芯恩"]:
                assert literal not in encoded, f"restricted 字面量不得出现在 PATCH 响应与学习信号：{literal}"


if __name__ == "__main__":
    unittest.main()
