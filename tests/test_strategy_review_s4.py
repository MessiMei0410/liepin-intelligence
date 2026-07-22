"""S4-3：策略复盘器 v1（规则版）测试。

口径：docs/ASA_PRD_S4_sourcing_strategy_agent_2026-07-23.md §5。
全部使用临时库 + 临时 KB fixture（运行时只读，绝不触碰真实知识库与生产 DB）。
覆盖：四判定分支 + 数据不足、revision_diff 结构、artifact 幂等 upsert、终局自动触发、
存量补生成（#154 第 2/3/4 轮口径）、API 200/404/幂等、restricted 不回泄。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from test_a_system_agent_v1 import AgentDbCase, fake_assessment  # noqa: E402
from a_system_agent import AgentService, FakeLLM  # noqa: E402
from a_system_agent import strategy_review  # noqa: E402
from asa_core.app import create_app  # noqa: E402


STRATEGY_V2_FIXTURE = {
    "schema_version": "strategy_v2",
    "input_level": "L2",
    "step1_job_essence": {"statement": "精密设备机械核心岗", "value_chain_role": "设备原厂", "confirmed_by": "consultant"},
    "step2_target_pool": [
        {
            "path": "same_layer", "tier": "T1",
            "companies": [{"name": "ASM", "source": "kb_profile", "confidence": "high"}],
            "rationale": "同层友商",
        }
    ],
    "step3_level_mapping": {"accepted_levels": ["高级工程师", "经理"], "calibration_rule": "按职责定档"},
    "step4_keyword_groups": [{"group": "core", "targets": "T1 友商", "terms": ["精密机械", "运动台"]}],
    "step5_expectation": {"expected_recall_per_tier": {"T1": 40}, "fallback_plan": "T1 不足放宽 T2"},
    "negative_rules": [
        {"type": "禁挖名单", "rule": "禁挖名单（在职保护）：青岛芯恩、福建晋华", "source": "restricted_client"},
    ],
    "consultant_edits": [],
    "archetype_id": "a1",
}

KB_CASE_FIXTURE = {
    "meta": {"case_id": "case_pengxinxu_fab"},
    "client_profile": {"name": "深圳市鹏新旭技术有限公司"},
    "restricted": {
        "banned_companies": ["青岛芯恩", "福建晋华"],
        "consultant_phone": "13912345678",
        "fee_rate": "费率23%",
        "scripts_redline": "话术红线MARKER_REDLINE_X7",
        "offer_amounts": ["offer金额 年薪120万"],
    },
}

FORBIDDEN_LITERALS = ["13912345678", "费率23%", "MARKER_REDLINE_X7", "120万", "话术红线"]

# KB 岗位原型 fixture：为复盘 revision_diff 提供 step2 公司 / step4 关键词候选
KB_ARCHETYPE_FIXTURE = {
    "job_archetype": {
        "archetype_id": "a1", "title": "精密设备机械", "client": "长越科技",
        "essence": "精密设备机械核心岗", "directions": [], "target_functions": [],
    },
    "target_company_pool": {
        "T2_customer_OEM": {
            "companies": [{"name": "下游X公司"}, {"name": "下游Y公司"}],
            "rationale": "逆向客户整机厂",
        }
    },
    "keyword_groups": [{"group": "kb_broad", "targets": "放宽", "terms": ["真空腔体", "传动机构"]}],
    "negative_rules": [],
    "level_mapping": {},
}


def _write_review_kb(base: Path, *, archetype: dict | None = KB_ARCHETYPE_FIXTURE, case: dict | None = KB_CASE_FIXTURE) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    if archetype is not None:
        (base / "seed_a1_mech_v1.json").write_text(json.dumps(archetype, ensure_ascii=False), encoding="utf-8")
    if case is not None:
        (base / "cases").mkdir(parents=True, exist_ok=True)
        (base / "cases" / "case_pengxinxu_fab_v1.json").write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    return base


def _funnel_row(
    channel: str = "liepin",
    status: str = "completed",
    recall: int = 0,
    unique: int = 0,
    intake_new: int = 0,
    assessed: int = 0,
    high: int = 0,
    detail: tuple[int, int, int] = (0, 0, 0),
    zero_attribution: str | None = None,
) -> dict:
    return {
        "channel": channel, "status": status, "query_count": 1,
        "recall_count": recall, "extracted_count": unique, "dedupe_count": 0,
        "unique_count": unique,
        "detail_complete": detail[0], "detail_partial": detail[1], "detail_failed": detail[2],
        "intake_duplicate_count": 0, "intake_new_count": intake_new,
        "assessed_count": assessed, "high_score_count": high,
        "zero_attribution": zero_attribution, "error": None,
    }


_DEFAULT_STRATEGY = object()


def _build(rows: list[dict], strategy: object = _DEFAULT_STRATEGY, **kwargs) -> dict:
    return strategy_review.build_strategy_review(
        workflow_id="wf-pure",
        strategy_doc=STRATEGY_V2_FIXTURE if strategy is _DEFAULT_STRATEGY else strategy,
        funnel_rows=rows,
        **kwargs,
    )


class VerdictBranchTest(unittest.TestCase):
    """复盘器四判定分支 + healthy：策略窄 / 执行渠道 / 高分率低 / 数据不足。"""

    def test_strategy_too_narrow_with_revision_diff(self) -> None:
        rows = [
            _funnel_row("liepin", recall=10, unique=8, intake_new=8, assessed=8, high=4, detail=(8, 0, 0)),
            _funnel_row("xsaas", recall=2, unique=2, intake_new=2, assessed=2, high=1, detail=(2, 0, 0)),
        ]
        review = _build(rows, pool_candidates=["X公司", "Y公司"], keyword_candidates=["真空腔体", "传动机构"])
        assert review["verdict"] == "strategy_too_narrow"
        assert "召回 12" in review["verdict_reason"] and "40" in review["verdict_reason"]
        assert review["evidence"]["expected_recall_total"] == 40
        assert review["evidence"]["recall_total"] == 12
        # revision_diff：step2 增列 T2 公司 + step4 替换关键词组，逐条带 reason 且可采纳/拒绝
        assert len(review["revision_diff"]) == 2
        add = review["revision_diff"][0]
        assert add["step"] == "step2_target_pool" and add["op"] == "add"
        assert add["tier"] == "T2" and add["companies"] == ["X公司", "Y公司"]
        replace = review["revision_diff"][1]
        assert replace["step"] == "step4_keyword_groups" and replace["op"] == "replace"
        assert replace["group"] == "core"
        assert set(replace["terms"]) <= {"真空腔体", "传动机构"}
        for index, diff in enumerate(review["revision_diff"], 1):
            assert diff["diff_id"] == f"diff-{index}"
            assert diff["reason"].strip(), "每条修订必须带 reason"
            assert diff["status"] == "pending", "每条修订必须可逐项采纳/拒绝（pending→accepted/rejected）"

    def test_strategy_too_narrow_without_kb_candidates_still_verdict(self) -> None:
        rows = [_funnel_row("liepin", recall=3, unique=3, intake_new=3, assessed=3, high=3, detail=(3, 0, 0))]
        review = _build(rows)
        assert review["verdict"] == "strategy_too_narrow"
        assert review["revision_diff"] == []
        assert any("待顾问" in note or "无可" in note for note in review["notes"])

    def test_execution_channel_zero_attribution(self) -> None:
        for attribution in ("session_expired", "page_structure_changed", "loading_incomplete"):
            with self.subTest(attribution=attribution):
                rows = [
                    _funnel_row("liepin", recall=30, unique=20, intake_new=20, assessed=20, high=10, detail=(20, 0, 0)),
                    _funnel_row("xsaas", status="blocked", zero_attribution=attribution),
                ]
                review = _build(rows)
                assert review["verdict"] == "execution_channel_issue"
                assert review["revision_diff"] == [], "执行/渠道问题不改策略"
                assert review["escalation"] is None
                finding = next(item for item in review["per_channel_findings"] if item["channel"] == "xsaas")
                assert finding["finding"] == "execution_issue"
                assert attribution in finding["note"]

    def test_execution_channel_detail_failed_ratio(self) -> None:
        rows = [
            _funnel_row("liepin", recall=30, unique=11, intake_new=11, assessed=11, high=5, detail=(2, 1, 8)),
        ]
        review = _build(rows)
        assert review["verdict"] == "execution_channel_issue"
        assert review["evidence"]["detail_failed_ratio"] == round(8 / 11, 4)
        assert review["per_channel_findings"][0]["finding"] == "execution_issue"

    def test_execution_precedes_recall_shortfall(self) -> None:
        # 召回短收能被渠道阻塞解释时，不误判策略（决策表序 2 优先于序 3）
        rows = [_funnel_row("liepin", status="blocked", recall=0, zero_attribution="loading_incomplete")]
        review = _build(rows)
        assert review["verdict"] == "execution_channel_issue"
        assert "策略问题" not in review["verdict_reason"]

    def test_quality_gap_with_escalation(self) -> None:
        rows = [
            _funnel_row("liepin", recall=30, unique=22, intake_new=22, assessed=20, high=2, detail=(22, 0, 0)),
        ]
        review = _build(rows)
        assert review["verdict"] == "quality_gap"
        assert review["evidence"]["high_score_rate"] == 0.1
        assert "高分率" in review["verdict_reason"] and "画像偏差" in review["verdict_reason"]
        escalation = review["escalation"]
        assert escalation["kind"] == "evaluation_issue_ticket"
        assert escalation["target"] == "evaluation" and escalation["status"] == "open"
        assert "评分偏差" in escalation["reason"]
        assert len(review["revision_diff"]) == 1
        diff = review["revision_diff"][0]
        assert diff["step"] == "step1_job_essence" and diff["op"] == "review"
        assert diff["reason"].strip() and diff["status"] == "pending"

    def test_quality_gap_threshold_configurable(self) -> None:
        rows = [
            _funnel_row("liepin", recall=30, unique=22, intake_new=22, assessed=20, high=2, detail=(22, 0, 0)),
        ]
        review = _build(rows, high_score_threshold=0.05)
        assert review["verdict"] == "healthy", "阈值可配置：0.1 ≥ 0.05 不判高分率偏低"
        assert review["thresholds"]["high_score_rate"] == 0.05

    def test_healthy(self) -> None:
        rows = [
            _funnel_row("liepin", recall=35, unique=20, intake_new=20, assessed=10, high=5, detail=(20, 0, 0)),
        ]
        review = _build(rows)
        assert review["verdict"] == "healthy"
        assert review["revision_diff"] == [] and review["escalation"] is None

    def test_insufficient_data_without_strategy(self) -> None:
        rows = [_funnel_row("liepin", recall=30, unique=5, intake_new=5, assessed=5, high=2, detail=(5, 0, 0))]
        review = _build(rows, strategy=None)
        assert review["verdict"] == "insufficient_data"
        assert "无 strategy_v2" in review["verdict_reason"]
        assert review["revision_diff"] == []
        assert review["degraded"] is True, "有漏斗行但无策略对象：降级保留证据"

    def test_insufficient_data_without_funnel_rows(self) -> None:
        review = _build([])
        assert review["verdict"] == "insufficient_data"
        assert "漏斗" in review["verdict_reason"]
        assert review["degraded"] is False
        degraded = _build([], assessment={"target": 10, "assessed": 8, "score_75_plus": 1, "verify_first": 0, "low_score": 0})
        assert degraded["verdict"] == "insufficient_data"
        assert degraded["degraded"] is True
        assert any("评估表" in note for note in degraded["notes"])


class ReviewDbCase(AgentDbCase):
    """公共 fixture：临时库（AgentDbCase 基座）+ 临时 KB + AgentService。"""

    def setUp(self) -> None:
        super().setUp()
        self.kb_temp = tempfile.TemporaryDirectory()
        kb_dir = _write_review_kb(Path(self.kb_temp.name) / "kb")
        self._old_kb_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(kb_dir)
        self.service = AgentService(self.db_path, FakeLLM(fake_assessment(), chat_text="测试回答"))

    def tearDown(self) -> None:
        self.service.close()
        if self._old_kb_env is None:
            os.environ.pop("ASA_KNOWLEDGE_BASE_DIR", None)
        else:
            os.environ["ASA_KNOWLEDGE_BASE_DIR"] = self._old_kb_env
        self.kb_temp.cleanup()
        super().tearDown()

    def wait_for(self, workflow_id: str, statuses: set[str], timeout: float = 8) -> dict:
        deadline = time.time() + timeout
        state = self.service.get_workflow(workflow_id)
        while time.time() < deadline and state["workflow"]["status"] not in statuses:
            time.sleep(0.03)
            state = self.service.get_workflow(workflow_id)
        return state

    def make_terminal_workflow(self, workflow_id: str, *, status: str = "blocked", created_at: str) -> None:
        outcome = "completed_pool_insufficient" if status == "blocked" else None
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO agent_goals (goal_id,objective,title,context_type,context_id,context_json,status,business_outcome,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"goal_{workflow_id}", "给长越科技机械高级工程师补充10位合适人选", "寻访",
                    "job", 10, '{"type":"job","id":10}', status, outcome, created_at, created_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO agent_workflows (workflow_id,goal_id,status,business_outcome,created_at,updated_at)
                VALUES (?,?,?,?,?,?)
                """,
                (workflow_id, f"goal_{workflow_id}", status, outcome, created_at, created_at),
            )
            conn.commit()
        finally:
            conn.close()

    def insert_strategy_artifact(self, workflow_id: str, v2: dict | None = None) -> None:
        doc = v2 or STRATEGY_V2_FIXTURE
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO agent_artifacts
                (artifact_id,goal_id,workflow_id,step_id,artifact_type,title,mime_type,content,metadata_json,validation_status)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"artifact_strategy_{workflow_id}", f"goal_{workflow_id}", workflow_id, None,
                    "search_strategy", "多渠道寻访策略", "text/markdown", "# 策略",
                    json.dumps({"strategy_v2": doc, "schema_version": "strategy_v2"}, ensure_ascii=False),
                    "passed",
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def insert_funnel(self, workflow_id: str, run_id: str, **kwargs) -> None:
        row = _funnel_row(**kwargs)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO agent_sourcing_funnel
                (run_id,workflow_id,job_id,client,job,channel,status,query_count,queries_json,
                 recall_count,extracted_count,dedupe_count,unique_count,
                 detail_complete,detail_partial,detail_failed,
                 intake_duplicate_count,intake_new_count,assessed_count,high_score_count,zero_attribution)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id, workflow_id, 10, "长越科技", "机械高级工程师",
                    row["channel"], row["status"], row["query_count"], "[]",
                    row["recall_count"], row["extracted_count"], row["dedupe_count"], row["unique_count"],
                    row["detail_complete"], row["detail_partial"], row["detail_failed"],
                    row["intake_duplicate_count"], row["intake_new_count"],
                    row["assessed_count"], row["high_score_count"], row["zero_attribution"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def review_row(self, workflow_id: str) -> dict | None:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM agent_artifacts WHERE workflow_id=? AND artifact_type='strategy_review' ORDER BY id DESC LIMIT 1",
                (workflow_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


class ReviewUpsertTest(ReviewDbCase):
    """artifact 幂等 upsert：同工作流同轮次重算覆盖，version 自增 + history。"""

    def test_rebuild_upserts_single_artifact_with_history(self) -> None:
        self.make_terminal_workflow("wf-up", created_at="2026-07-20 10:00:00")
        self.insert_strategy_artifact("wf-up")
        self.insert_funnel("wf-up", "run-1", channel="liepin", recall=10, unique=8, intake_new=8, assessed=8, high=4, detail=(8, 0, 0))

        first = self.service.rebuild_strategy_review("wf-up")
        assert first["ok"] is True and first["review"]["version"] == 1
        assert first["review"]["verdict"] == "strategy_too_narrow"

        # 重算（同工作流同轮次）：覆盖旧复盘而非新增
        self.insert_funnel("wf-up", "run-2", channel="xsaas", status="blocked", zero_attribution="session_expired")
        second = self.service.rebuild_strategy_review("wf-up")
        assert second["artifact_id"] == first["artifact_id"], "upsert 保持 artifact_id 稳定"
        assert second["review"]["version"] == 2
        assert second["review"]["verdict"] == "execution_channel_issue", "重算按最新漏斗行判定"
        history = second["review"]["history"]
        assert len(history) == 1
        assert history[0]["version"] == 1 and history[0]["verdict"] == "strategy_too_narrow"

        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM agent_artifacts WHERE workflow_id=? AND artifact_type='strategy_review'",
                ("wf-up",),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1, "幂等 upsert：同一工作流只保留一条 strategy_review"

        loaded = self.service.get_strategy_review("wf-up")
        assert loaded["review"]["version"] == 2
        assert loaded["review"]["verdict"] == "execution_channel_issue"
        assert loaded["artifact_id"] == first["artifact_id"]

    def test_get_strategy_review_404_semantics(self) -> None:
        self.make_terminal_workflow("wf-empty", created_at="2026-07-20 10:00:00")
        with self.assertRaises(LookupError):
            self.service.get_strategy_review("wf-empty")
        with self.assertRaises(LookupError):
            self.service.get_strategy_review("wf-missing")

    def test_rebuild_guards(self) -> None:
        with self.assertRaises(LookupError):
            self.service.rebuild_strategy_review("wf-missing")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO agent_goals (goal_id,objective,title,context_json,status) VALUES ('goal_run','寻访','寻访','{}','running')"
            )
            conn.execute("INSERT INTO agent_workflows (workflow_id,goal_id,status) VALUES ('wf-run','goal_run','running')")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(ValueError):
            self.service.rebuild_strategy_review("wf-run")


class AutoTriggerTest(ReviewDbCase):
    """终局自动触发：_finish() 后对寻访类工作流自动生成复盘。"""

    def drive_sourcing_to_terminal(self, objective: str) -> tuple[str, dict]:
        result = self.service.create_goal(objective, {"type": "job", "id": 10})
        workflow_id = result["workflow"]["workflow_id"]
        self.service.start_workflow(workflow_id)
        waiting = self.wait_for(workflow_id, {"waiting_approval", "failed"})
        approval = next(item for item in waiting["approvals"] if item["status"] == "pending")
        self.service.decide_workflow_approval(approval["approval_id"], "approve")
        external = self.wait_for(workflow_id, {"waiting_external", "failed"})
        external_step = next(step for step in external["steps"] if step["status"] == "waiting_external")
        self.service.complete_external_workflow_step(
            external_step["id"],
            {
                "verified": True,
                "run_id": "source-test",
                "channel_runs": [{"channel": "liepin", "status": "blocked"}],
                "intake": {"accepted_count": 0},
                "audit": {"ok": True},
            },
        )
        return workflow_id, self.wait_for(workflow_id, {"blocked", "completed", "failed"})

    def test_blocked_terminal_generates_insufficient_review(self) -> None:
        # #154 第 2/3 轮口径：终局 blocked 且该轮无漏斗行 → 自动生成 insufficient_data 复盘
        workflow_id, state = self.drive_sourcing_to_terminal("给长越科技机械高级工程师补充10位合适人选")
        assert state["workflow"]["status"] == "blocked"
        row = self.review_row(workflow_id)
        assert row, "寻访类工作流终局后必须自动生成 strategy_review artifact"
        review = json.loads(row["metadata_json"])
        assert review["verdict"] == "insufficient_data"
        assert "漏斗" in review["verdict_reason"]
        assert review["generator"] == "rule_v1"
        assert review["version"] == 1
        assert review["round_index"] >= 1
        events = [event["event_type"] for event in self.service.get_workflow(workflow_id)["events"]]
        assert "strategy_review_generated" in events

    def test_terminal_review_uses_available_funnel_rows(self) -> None:
        result = self.service.create_goal("给长越科技机械高级工程师补充10位合适人选", {"type": "job", "id": 10})
        workflow_id = result["workflow"]["workflow_id"]
        # 终局前写入当轮漏斗行（模拟执行链路已落漏斗）
        self.insert_funnel(workflow_id, "run-pre", channel="xsaas", status="blocked", zero_attribution="session_expired")
        self.insert_funnel(workflow_id, "run-pre2", channel="liepin", recall=25, unique=15, intake_new=15, assessed=15, high=7, detail=(15, 0, 0))
        self.service.start_workflow(workflow_id)
        waiting = self.wait_for(workflow_id, {"waiting_approval", "failed"})
        approval = next(item for item in waiting["approvals"] if item["status"] == "pending")
        self.service.decide_workflow_approval(approval["approval_id"], "approve")
        external = self.wait_for(workflow_id, {"waiting_external", "failed"})
        external_step = next(step for step in external["steps"] if step["status"] == "waiting_external")
        self.service.complete_external_workflow_step(
            external_step["id"],
            {
                "verified": True,
                "run_id": "source-test",
                "channel_runs": [{"channel": "liepin", "status": "blocked"}],
                "intake": {"accepted_count": 0},
                "audit": {"ok": True},
            },
        )
        state = self.wait_for(workflow_id, {"blocked", "completed", "failed"})
        assert state["workflow"]["status"] in {"blocked", "completed"}
        row = self.review_row(workflow_id)
        assert row, "终局后必须生成复盘"
        review = json.loads(row["metadata_json"])
        assert review["verdict"] == "execution_channel_issue"
        channels = {item["channel"] for item in review["per_channel_findings"]}
        assert channels == {"liepin", "xsaas"}


class BackfillTest(ReviewDbCase):
    """存量补生成（#154 第 2/3/4 轮验收样本口径）：第 2/3 轮无漏斗行 → insufficient_data，
    第 4 轮有漏斗行 → 按证据判定；轮次编号按岗位时间线。"""

    def test_backfill_rounds_2_3_insufficient_and_round_4_judged(self) -> None:
        self.make_terminal_workflow("wf-r2", created_at="2026-07-20 10:00:00")
        self.make_terminal_workflow("wf-r3", created_at="2026-07-21 10:00:00")
        self.make_terminal_workflow("wf-r4", created_at="2026-07-22 10:00:00")
        for workflow_id in ("wf-r2", "wf-r3", "wf-r4"):
            self.insert_strategy_artifact(workflow_id)
        self.insert_funnel("wf-r4", "run-4", channel="liepin", recall=8, unique=6, intake_new=6, assessed=6, high=3, detail=(6, 0, 0))

        r2 = self.service.rebuild_strategy_review("wf-r2")["review"]
        r3 = self.service.rebuild_strategy_review("wf-r3")["review"]
        r4 = self.service.rebuild_strategy_review("wf-r4")["review"]

        assert r2["verdict"] == "insufficient_data" and r2["round_index"] == 1
        assert r3["verdict"] == "insufficient_data" and r3["round_index"] == 2
        assert "漏斗" in r3["verdict_reason"]
        assert r4["verdict"] == "strategy_too_narrow" and r4["round_index"] == 3
        assert r4["revision_diff"], "策略过窄必须给出修订 diff"
        assert r4["evidence"]["recall_total"] == 8

    def test_review_does_not_echo_restricted_literals(self) -> None:
        # 策略对象带 restricted 约束（禁挖名单，内部判断用）；复盘输入含它但输出绝不回泄
        self.make_terminal_workflow("wf-sec", created_at="2026-07-20 10:00:00")
        self.insert_strategy_artifact("wf-sec")
        self.insert_funnel("wf-sec", "run-1", channel="liepin", recall=5, unique=4, intake_new=4, assessed=4, high=2, detail=(4, 0, 0))
        result = self.service.rebuild_strategy_review("wf-sec")
        assert result["review"]["verdict"] == "strategy_too_narrow"
        payload = self.service.get_strategy_review("wf-sec")
        encoded = json.dumps(payload, ensure_ascii=False)
        for literal in FORBIDDEN_LITERALS + ["青岛芯恩", "福建晋华"]:
            assert literal not in encoded, f"restricted 字面量不得出现在复盘输出：{literal}"


class StrategyReviewApiTest(unittest.TestCase):
    """API：GET 最新复盘（含 revision_diff）200/404；POST rebuild 幂等重放。"""

    def setUp(self) -> None:
        self.kb_temp = tempfile.TemporaryDirectory()
        kb_dir = _write_review_kb(Path(self.kb_temp.name) / "kb")
        self._old_kb_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(kb_dir)

    def tearDown(self) -> None:
        if self._old_kb_env is None:
            os.environ.pop("ASA_KNOWLEDGE_BASE_DIR", None)
        else:
            os.environ["ASA_KNOWLEDGE_BASE_DIR"] = self._old_kb_env
        self.kb_temp.cleanup()

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

    def _create_db(self, db_path: Path) -> None:
        conn = sqlite3.connect(db_path)
        conn.executescript(self.API_SCHEMA)
        conn.execute("INSERT INTO clients VALUES (1,'长越科技')")
        conn.execute("INSERT INTO jobs VALUES (10,1,'机械高级工程师')")
        conn.commit()
        conn.close()

    def _seed_review(self, db_path: Path, workflow_id: str = "wf-api") -> None:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            INSERT INTO agent_goals (goal_id,objective,title,context_type,context_id,context_json,status,business_outcome)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                f"goal_{workflow_id}", "给长越科技机械高级工程师补充10位合适人选", "寻访",
                "job", 10, '{"type":"job","id":10}', "blocked", "completed_pool_insufficient",
            ),
        )
        conn.execute(
            "INSERT INTO agent_workflows (workflow_id,goal_id,status,business_outcome) VALUES (?,?,?,?)",
            (workflow_id, f"goal_{workflow_id}", "blocked", "completed_pool_insufficient"),
        )
        conn.execute(
            """
            INSERT INTO agent_artifacts
            (artifact_id,goal_id,workflow_id,step_id,artifact_type,title,mime_type,content,metadata_json,validation_status)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"artifact_strategy_{workflow_id}", f"goal_{workflow_id}", workflow_id, None,
                "search_strategy", "多渠道寻访策略", "text/markdown", "# 策略",
                json.dumps({"strategy_v2": STRATEGY_V2_FIXTURE}, ensure_ascii=False), "passed",
            ),
        )
        conn.execute(
            """
            INSERT INTO agent_sourcing_funnel
            (run_id,workflow_id,job_id,client,job,channel,status,query_count,queries_json,
             recall_count,extracted_count,dedupe_count,unique_count,
             detail_complete,detail_partial,detail_failed,intake_new_count,assessed_count,high_score_count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "run-api", workflow_id, 10, "长越科技", "机械高级工程师", "liepin", "completed", 1, "[]",
                10, 8, 0, 8, 8, 0, 0, 8, 8, 4,
            ),
        )
        conn.execute(
            """
            INSERT INTO agent_goals (goal_id,objective,title,context_json,status)
            VALUES ('goal_running','寻访','寻访','{}','running')
            """
        )
        conn.execute("INSERT INTO agent_workflows (workflow_id,goal_id,status) VALUES ('wf-running','goal_running','running')")
        conn.execute(
            """
            INSERT INTO agent_goals (goal_id,objective,title,context_type,context_id,context_json,status,business_outcome)
            VALUES ('goal_noreview','寻访','寻访','job',10,'{"type":"job","id":10}','blocked','completed_pool_insufficient')
            """
        )
        conn.execute(
            "INSERT INTO agent_workflows (workflow_id,goal_id,status,business_outcome) VALUES ('wf-noreview','goal_noreview','blocked','completed_pool_insufficient')"
        )
        conn.commit()
        conn.close()

    def test_get_200_with_revision_diff_and_rebuild_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "asa.db"
            self._create_db(db_path)
            app = create_app(db_path=db_path, start_legacy=False)
            self._seed_review(db_path)
            with TestClient(app) as client:
                missing = client.get("/api/v1/workflows/wf-missing/strategy-review")
                assert missing.status_code == 404, "工作流不存在 → 404"
                none_yet = client.get("/api/v1/workflows/wf-noreview/strategy-review")
                assert none_yet.status_code == 404, "工作流存在但无复盘 → 404"

                headers = {"Idempotency-Key": "rebuild-key-1"}
                body = {"request_id": "req-rebuild-1"}
                first = client.post("/api/v1/workflows/wf-api/strategy-review/rebuild", json=body, headers=headers)
                assert first.status_code == 200, first.text
                payload = first.json()
                assert payload["ok"] is True
                assert payload["review"]["verdict"] == "strategy_too_narrow"
                assert payload["review"]["version"] == 1
                assert payload["receipt"]["idempotent_replay"] is False

                replay = client.post("/api/v1/workflows/wf-api/strategy-review/rebuild", json=body, headers=headers)
                assert replay.status_code == 200
                replayed = replay.json()
                assert replayed["receipt"]["idempotent_replay"] is True, "同 Idempotency-Key 重放首次响应"
                assert replayed["review"]["version"] == 1, "重放不得重复重算（version 不自增）"

                conflict = client.post(
                    "/api/v1/workflows/wf-api/strategy-review/rebuild",
                    json={"request_id": "req-rebuild-2"}, headers=headers,
                )
                assert conflict.status_code == 409, "同键不同负载 → 409"

                non_terminal = client.post(
                    "/api/v1/workflows/wf-running/strategy-review/rebuild",
                    json={"request_id": "req-rebuild-3"}, headers={"Idempotency-Key": "rebuild-key-3"},
                )
                assert non_terminal.status_code == 409, "非终局工作流不能生成复盘"

                got = client.get("/api/v1/workflows/wf-api/strategy-review")
                assert got.status_code == 200
                data = got.json()
                assert data["ok"] is True and data["artifact_id"]
                review = data["review"]
                assert review["verdict"] == "strategy_too_narrow"
                assert review["verdict_label"] and review["verdict_reason"]
                assert review["per_channel_findings"], "GET 必须含逐渠道发现"
                assert review["revision_diff"], "GET 必须含 revision_diff"
                for diff in review["revision_diff"]:
                    assert diff["reason"].strip() and diff["status"] == "pending"
                assert review["evidence"]["recall_total"] == 10

                # rebuild 后再 GET 应为最新版（version 自增）
                again = client.post(
                    "/api/v1/workflows/wf-api/strategy-review/rebuild",
                    json={"request_id": "req-rebuild-4"}, headers={"Idempotency-Key": "rebuild-key-4"},
                )
                assert again.status_code == 200
                assert again.json()["review"]["version"] == 2
                refreshed = client.get("/api/v1/workflows/wf-api/strategy-review").json()
                assert refreshed["review"]["version"] == 2
                assert len(refreshed["review"]["history"]) == 1

    def test_api_output_never_echoes_restricted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "asa.db"
            self._create_db(db_path)
            app = create_app(db_path=db_path, start_legacy=False)
            self._seed_review(db_path)
            with TestClient(app) as client:
                client.post(
                    "/api/v1/workflows/wf-api/strategy-review/rebuild",
                    json={"request_id": "req-sec-1"}, headers={"Idempotency-Key": "sec-key-1"},
                )
                response = client.get("/api/v1/workflows/wf-api/strategy-review")
                assert response.status_code == 200
                encoded = json.dumps(response.json(), ensure_ascii=False)
                for literal in FORBIDDEN_LITERALS + ["青岛芯恩"]:
                    assert literal not in encoded, f"restricted 字面量不得出现在复盘 API：{literal}"


if __name__ == "__main__":
    unittest.main()
