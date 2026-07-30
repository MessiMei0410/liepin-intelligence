from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from a_system_agent import AgentService, FakeLLM, query_builders  # noqa: E402
from a_system_agent.capability_runtime import (  # noqa: E402
    ZERO_RESULT_ATTRIBUTIONS,
    _command_failure_summary,
    adapt_channel_queries,
    classify_zero_result,
)
from a_system_agent.schema import ensure_schema  # noqa: E402
from asa_core.app import create_app  # noqa: E402


class AdaptChannelQueriesTest(unittest.TestCase):
    """顾问规则（2026-07-23）：X-SaaS 吃不下多词组合；猎聘 AND 语义滤掉只写一两个词的简历。"""

    def test_short_queries_unchanged(self) -> None:
        self.assertEqual(
            adapt_channel_queries(["DrMOS", "多相控制器", "MPS 矽力杰"], max_terms=2, max_count=8),
            ["DrMOS", "多相控制器", "MPS 矽力杰"],
        )

    def test_dense_query_expands_to_anchor_pairs(self) -> None:
        self.assertEqual(
            adapt_channel_queries(["多相控制器 DrMOS POL TME FAE"], max_terms=2, max_count=8),
            ["多相控制器 DrMOS", "多相控制器 POL", "多相控制器 TME", "多相控制器 FAE"],
        )

    def test_dedupe_preserves_order(self) -> None:
        self.assertEqual(
            adapt_channel_queries(["MPS 电源 市场", "MPS 电源"], max_terms=2, max_count=8),
            ["MPS 电源", "MPS 市场"],
        )

    def test_count_cap_keeps_priority_order(self) -> None:
        out = adapt_channel_queries(["a b c d e f g h i j"], max_terms=2, max_count=4)
        self.assertEqual(out, ["a b", "a c", "a d", "a e"])

    def test_empty_and_malformed_robust(self) -> None:
        self.assertEqual(adapt_channel_queries([], max_terms=2, max_count=8), [])
        self.assertEqual(adapt_channel_queries(["", None, "  "], max_terms=2, max_count=8), [])

    def test_nested_audit_json_reports_failed_check_instead_of_truncated_middle(self) -> None:
        guard = {
            "ok": False,
            "checks": [
                {"ok": True, "check": "visible_nav"},
                {"ok": False, "check": "stopped_candidates_have_no_open_followups", "message": "已停止人选仍有开放任务"},
            ],
        }
        wrapper = {"cmd": ["python", "guard.py"], "returncode": 1, "stdout": json.dumps(guard, ensure_ascii=False), "stderr": ""}

        summary, detail = _command_failure_summary(json.dumps(wrapper, ensure_ascii=False), "", 1)

        self.assertEqual(summary, "已停止人选仍有开放任务")
        self.assertEqual(detail["failed_checks"][0]["check"], "stopped_candidates_have_no_open_followups")

    def test_dict_entries_use_query_field(self) -> None:
        # round6 实证：LLM 策略步骤产出 {evidence, purpose, query, round} 字典项，
        # 直接 str() 会把 Python repr 当查询词（"{'evidence': 'purpose':" 垃圾查询）
        items = [
            {"evidence": "岗位要求熟悉多相控制器", "purpose": "精准锁定", "query": "多相控制器 DrMOS POL TME", "round": "core"},
            {"query": "MPS 矽力杰"},
            {"evidence": "缺 query 字段"},
            "杰华特 技术市场 产品定义",
        ]
        self.assertEqual(
            adapt_channel_queries(items, max_terms=2, max_count=8),
            ["多相控制器 DrMOS", "多相控制器 POL", "多相控制器 TME", "MPS 矽力杰", "杰华特 技术市场", "杰华特 产品定义"],
        )

    def test_company_pairs_never_combined(self) -> None:
        # 顾问规则（round7）：两个公司名组合语义必错——公司词只与非公司词配对或单独成组
        out = adapt_channel_queries(
            ["MPS 矽力杰 杰华特 FAE AE"], max_terms=2, max_count=8,
            company_terms={"mps", "矽力杰", "杰华特"},
        )
        self.assertNotIn("MPS 矽力杰", out)
        self.assertNotIn("矽力杰 杰华特", out)
        self.assertEqual(out, ["MPS FAE", "矽力杰 FAE", "杰华特 FAE", "FAE AE"])

    def test_company_only_query_goes_solo(self) -> None:
        out = adapt_channel_queries(["MPS 矽力杰"], max_terms=2, max_count=8, company_terms={"mps", "矽力杰"})
        self.assertEqual(out, ["MPS", "矽力杰"])

    def test_single_company_pairs_with_first_function_term(self) -> None:
        out = adapt_channel_queries(["杰华特 技术市场 产品定义"], max_terms=2, max_count=8, company_terms={"杰华特"})
        self.assertEqual(out, ["杰华特 技术市场", "技术市场 产品定义"])

    def test_channel_presets(self) -> None:
        from a_system_agent.capability_runtime import (
            LIEPIN_QUERY_MAX_COUNT,
            LIEPIN_QUERY_MAX_TERMS,
            XSAAS_QUERY_MAX_COUNT,
            XSAAS_QUERY_MAX_TERMS,
        )
        self.assertEqual((XSAAS_QUERY_MAX_TERMS, XSAAS_QUERY_MAX_COUNT), (2, 8))
        self.assertEqual((LIEPIN_QUERY_MAX_TERMS, LIEPIN_QUERY_MAX_COUNT), (2, 6))


FUNNEL_COLUMNS = {
    "run_id", "workflow_id", "job_id", "client", "job", "channel", "status",
    "query_count", "queries_json", "recall_count", "extracted_count", "dedupe_count",
    "unique_count", "detail_complete", "detail_partial", "detail_failed",
    "intake_duplicate_count", "intake_new_count", "assessed_count", "high_score_count",
    "zero_attribution", "error", "created_at", "updated_at",
}


class FunnelSchemaTest(unittest.TestCase):
    def test_ensure_schema_creates_funnel_table_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            conn = sqlite3.connect(Path(temp) / "agent.db")
            ensure_schema(conn)
            conn.execute(
                "INSERT INTO agent_sourcing_funnel(run_id,workflow_id,channel,status) VALUES ('r1','wf-1','liepin','completed')"
            )
            conn.commit()
            ensure_schema(conn)  # 第二次执行不得报错也不得丢数据
            columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_sourcing_funnel)")}
            assert FUNNEL_COLUMNS <= columns
            row = conn.execute("SELECT run_id,status FROM agent_sourcing_funnel").fetchone()
            assert row == ("r1", "completed")
            conn.close()


class ClassifyZeroResultTest(unittest.TestCase):
    def test_all_branches_return_defined_enum(self) -> None:
        cases = {
            "session_expired": [
                ("xsaas", "blocked", {"ok": False, "error": "X-SAAS_LOGIN_REQUIRED: 未找到已登录的 X-SaaS 页面"}),
                ("xsaas", "blocked", {"ok": False, "error": "X-SAAS_LOGIN_REQUIRED: 详情页登录态失效"}),
                ("liepin", "failed", {"error": "猎聘登录已过期，请在 Chrome 里登录猎聘后再继续。"}),
            ],
            "compliance_wall": [
                ("liepin", "failed", {"error": "猎聘命中安全合规承诺函（合规墙），请在 Chrome 里阅读并确认后再继续。"}),
                ("liepin", "failed", {"error": "redirect to https://h.liepin.com/user/compliancecommitment"}),
            ],
            "loading_incomplete": [
                ("xsaas", "blocked", {"ok": False, "error": "X-SaaS 候选人列表加载超时"}),
                ("xsaas", "completed", {"ok": True, "rounds": [{"query": "q", "status": "stale_query", "result_count": 8, "extracted_count": 0}]}),
                ("xsaas", "completed", {"ok": True, "rounds": [{"query": "q", "status": "failed", "reason": "settle_timeout"}]}),
                ("xsaas", "completed", {"ok": True, "rounds": [{"query": "q", "status": "skipped", "reason": "settle_timeout", "attempts": 2}]}),
            ],
            "page_structure_changed": [
                ("xsaas", "completed", {"ok": True, "rounds": [{"query": "q", "status": "failed", "reason": "search_controls_missing"}]}),
            ],
            "parse_failure": [
                ("xsaas", "completed", {"ok": True, "rounds": [{"query": "q", "status": "completed", "result_count": 12, "extracted_count": 0}]}),
                ("liepin", "completed", {"ok": True, "rounds": [{"query": "q", "result_count": 5, "extracted_count": 0}]}),
            ],
            "no_results": [
                ("xsaas", "completed", {"ok": True, "rounds": [{"query": "q", "status": "completed", "result_count": 0, "extracted_count": 0}]}),
                # 平台有结果且正常抓取，但全部被评分门槛/排重过滤
                ("liepin", "completed", {"ok": True, "rounds": [{"query": "q", "result_count": 30, "extracted_count": 24}]}),
            ],
            "unknown": [
                ("xsaas", "blocked", {"ok": False, "error": "X-SaaS CDP 执行超时"}),
                ("xsaas", "completed", {"ok": True, "candidates": 0}),  # 无 per-query 明细
                ("liepin", "failed", {"error": "some opaque cdp failure"}),
            ],
        }
        for expected, entries in cases.items():
            for channel, status, result in entries:
                with self.subTest(expected=expected, channel=channel, status=status, result=result):
                    assert classify_zero_result(channel, status, result) == expected
        for channel, status, result in [entry for entries in cases.values() for entry in entries]:
            assert classify_zero_result(channel, status, result) in ZERO_RESULT_ATTRIBUTIONS

    def test_query_build_error_round7_nested_keyword_concat(self) -> None:
        # S4-3c-1 契约：workflow_bcab82502825 第 7 轮真实形态——X-SaaS selected_query
        # 条件嵌套拼接（"关键字："≥2 次），当时只能报 unknown。
        result = {
            "ok": True,
            "rounds": [
                {"query": "MPS 矽力杰", "status": "completed", "result_count": 0, "extracted_count": 0,
                 "selected_query": "MPS 矽力杰 关键字：MPS 杰华特 关键字：MPS TME"},
                {"query": "MPS TME", "status": "completed", "result_count": 0, "extracted_count": 0,
                 "selected_query": "MPS TME 关键字：MPS 矽力杰 关键字：MPS 杰华特"},
            ],
        }
        assert classify_zero_result("xsaas", "completed", result) == "query_build_error"

    def test_query_build_error_single_query_two_companies(self) -> None:
        # 单查询含 ≥2 个公司名（一人不可能同时在两家公司，组合语义必错）
        result = {"ok": True, "rounds": [{"query": "MPS 矽力杰", "status": "completed", "result_count": 0, "extracted_count": 0}]}
        assert classify_zero_result("xsaas", "completed", result, company_vocab={"mps", "矽力杰"}) == "query_build_error"
        # 词表为空时该信号不启用，回落常规判定
        assert classify_zero_result("xsaas", "completed", result) == "no_results"

    def test_query_build_error_repr_fragment_and_empty_query(self) -> None:
        # round6 真实形态：{evidence, purpose, query, round} 字典被 str() 当查询词
        result = {"ok": True, "rounds": [{"query": "{'evidence': 'purpose':", "status": "completed", "result_count": 0, "extracted_count": 0}]}
        assert classify_zero_result("xsaas", "completed", result) == "query_build_error"
        empty = {"ok": True, "rounds": [{"query": "  ", "status": "completed", "result_count": 0, "extracted_count": 0}]}
        assert classify_zero_result("xsaas", "completed", empty) == "query_build_error"

    def test_query_build_error_condition_accumulation(self) -> None:
        # 查询间条件累加：后一条页面回显查询完整包含前一条且更长（条件未重置）
        result = {
            "ok": True,
            "rounds": [
                {"query": "MPS", "selected_query": "MPS", "status": "completed", "result_count": 0, "extracted_count": 0},
                {"query": "矽力杰", "selected_query": "MPS 矽力杰", "status": "completed", "result_count": 0, "extracted_count": 0},
            ],
        }
        assert classify_zero_result("xsaas", "completed", result) == "query_build_error"

    def test_pool_saturated_high_dedupe_rate(self) -> None:
        # F3 实证形态：召回正常但 dedupe_rate 119/120 > 90%
        result = {"ok": True, "rounds": [{"query": "q", "status": "completed", "result_count": 120, "extracted_count": 120}]}
        assert classify_zero_result("liepin", "completed", result, dedupe_count=119) == "pool_saturated"
        # 边界：恰好 90% 不触发；dedupe_count 未传（旧调用方）保持 no_results
        assert classify_zero_result("liepin", "completed", result, dedupe_count=108) == "no_results"
        assert classify_zero_result("liepin", "completed", result) == "no_results"

    def test_attribution_priority_order(self) -> None:
        # 执行类 > query_build_error：session/结构/stale 信号先判
        blocked = {
            "ok": False,
            "error": "X-SAAS_LOGIN_REQUIRED: 未找到已登录的 X-SaaS 页面",
            "rounds": [{"query": "{'evidence':", "result_count": 0, "extracted_count": 0}],
        }
        assert classify_zero_result("xsaas", "blocked", blocked) == "session_expired"
        structure = {"ok": True, "rounds": [{"query": "", "status": "failed", "reason": "search_controls_missing"}]}
        assert classify_zero_result("xsaas", "completed", structure) == "page_structure_changed"
        stale = {"ok": True, "rounds": [{"query": "MPS 矽力杰", "status": "stale_query", "result_count": 0, "extracted_count": 0}]}
        assert classify_zero_result("xsaas", "completed", stale, company_vocab={"mps", "矽力杰"}) == "loading_incomplete"
        # query_build_error > pool_saturated
        both = {"ok": True, "rounds": [{"query": "MPS 矽力杰", "status": "completed", "result_count": 120, "extracted_count": 120}]}
        assert classify_zero_result("liepin", "completed", both, dedupe_count=119, company_vocab={"mps", "矽力杰"}) == "query_build_error"


class ValidateExternalResultQualityTest(unittest.TestCase):
    def _result(self, channel_runs: list[dict]) -> dict:
        return {
            "verified": True,
            "channel_runs": channel_runs,
            "intake": {"applied": {"inserted": 0}},
            "audit": {"ok": True},
        }

    def test_existing_guards_still_raise(self) -> None:
        validate = AgentService.validate_external_result
        with self.assertRaises(ValueError):
            validate("multi_channel_sourcing", {"verified": False})
        with self.assertRaises(ValueError):
            validate("multi_channel_sourcing", {"verified": True, "channel_runs": [], "intake": {}, "audit": {"ok": True}})
        with self.assertRaises(ValueError):
            validate("multi_channel_sourcing", {"verified": True, "channel_runs": [{"channel": "x"}], "intake": {"a": 1}, "audit": {"ok": False}})

    def test_zero_candidates_with_attribution_marked_zero_attributed(self) -> None:
        result = self._result([
            {"channel": "liepin", "status": "completed", "result": {"ok": True, "candidates": 0}, "zero_attribution": "no_results"},
        ])
        AgentService.validate_external_result("multi_channel_sourcing", result)
        assert result["channel_runs"][0]["quality"] == "zero_attributed"

    def test_zero_candidates_without_attribution_marked_zero_unknown(self) -> None:
        result = self._result([
            {"channel": "xsaas", "status": "completed", "result": {"ok": True, "candidates": 0}},
            {"channel": "liepin", "status": "completed", "result": {"ok": True, "candidates": 0}, "zero_attribution": "unknown"},
        ])
        AgentService.validate_external_result("multi_channel_sourcing", result)
        assert result["channel_runs"][0]["quality"] == "zero_unknown"
        assert result["channel_runs"][1]["quality"] == "zero_unknown"

    def test_nonzero_and_blocked_runs_are_not_marked(self) -> None:
        result = self._result([
            {"channel": "liepin", "status": "completed", "result": {"ok": True, "candidates": 7}},
            {"channel": "xsaas", "status": "blocked", "result": {"ok": False, "status": "blocked", "error": "X-SAAS_LOGIN_REQUIRED"}},
        ])
        AgentService.validate_external_result("multi_channel_sourcing", result)
        assert "quality" not in result["channel_runs"][0]
        assert "quality" not in result["channel_runs"][1]


class SourcingFunnelExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "agent.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE clients(id INTEGER PRIMARY KEY,name TEXT);
            CREATE TABLE jobs(id INTEGER PRIMARY KEY,client_id INTEGER,title TEXT);
            CREATE TABLE search_experiments(
                id INTEGER PRIMARY KEY, client TEXT, position TEXT, channel TEXT, query TEXT,
                result_count INTEGER, viewed_count INTEGER, recommended_count INTEGER,
                positive_reply_count INTEGER, noise_notes TEXT,
                run_time TEXT, created_at TEXT, updated_at TEXT
            );
            """
        )
        conn.execute("INSERT INTO clients VALUES (1,'长越科技')")
        conn.execute("INSERT INTO jobs VALUES (10,1,'机械高级工程师')")
        conn.commit()
        conn.close()
        self.service = AgentService(self.db_path, FakeLLM({}))

    def tearDown(self) -> None:
        self.service.close()
        self.temp.cleanup()

    def _funnel_rows(self) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute("SELECT * FROM agent_sourcing_funnel ORDER BY channel").fetchall()
        finally:
            conn.close()

    def _install_runners(
        self,
        *,
        liepin_result: dict | Exception,
        liepin_candidates: list[dict] | None = None,
        liepin_raw_candidates: list[dict] | None = None,
        xsaas_result: dict | Exception,
        xsaas_candidates: list[dict] | None = None,
        xsaas_raw_candidates: list[dict] | None = None,
        intake_apply: dict | Exception | None = None,
    ) -> None:
        runtime = self.service.capability_runtime

        def fake_run_json(command: list[str], timeout: int = 300) -> dict:
            cmd = [str(part) for part in command]
            if "--json-output" in cmd:
                if isinstance(liepin_result, Exception):
                    raise liepin_result
                out = Path(cmd[cmd.index("--json-output") + 1])
                out.write_text(json.dumps(liepin_candidates or [], ensure_ascii=False), encoding="utf-8")
                if "--raw-json-output" in cmd:
                    raw_out = Path(cmd[cmd.index("--raw-json-output") + 1])
                    raw_out.write_text(
                        json.dumps(liepin_raw_candidates if liepin_raw_candidates is not None else (liepin_candidates or []), ensure_ascii=False),
                        encoding="utf-8",
                    )
                return liepin_result
            if "--output" in cmd:
                if isinstance(xsaas_result, Exception):
                    raise xsaas_result
                out = Path(cmd[cmd.index("--output") + 1])
                out.write_text(json.dumps(xsaas_candidates or [], ensure_ascii=False), encoding="utf-8")
                if "--raw-output" in cmd:
                    raw_out = Path(cmd[cmd.index("--raw-output") + 1])
                    raw_out.write_text(
                        json.dumps(xsaas_raw_candidates if xsaas_raw_candidates is not None else (xsaas_candidates or []), ensure_ascii=False),
                        encoding="utf-8",
                    )
                return xsaas_result
            if "intake" in cmd:
                if "--apply" in cmd:
                    if isinstance(intake_apply, Exception):
                        raise intake_apply
                    return intake_apply or {"staged": {"accepted": []}, "intake": {"inserted": 0, "receipts": []}}
                return {"staged": {"accepted_count": 0}, "intake": {"applied": False, "inserted": 0}}
            raise AssertionError(f"unexpected command: {cmd}")

        runtime._run_json = fake_run_json  # type: ignore[method-assign]
        runtime._run = lambda command, timeout=300: subprocess.CompletedProcess(command, 0, stdout="sync ok", stderr="")  # type: ignore[method-assign]

    def _request(self) -> dict:
        cells = [
            {
                "cell_id": "qpc_test_liepin",
                "channel": "liepin",
                "query": "机械 设计",
                "locations": [], "levels": [], "scenarios": [], "priority": 1,
                "provenance": [{"kind": "keyword_group", "group": "mechanical"}],
            },
            {
                "cell_id": "qpc_test_xsaas",
                "channel": "xsaas",
                "query": "机械工程师",
                "locations": [], "levels": [], "scenarios": [], "priority": 1,
                "provenance": [{"kind": "keyword_group", "group": "mechanical"}],
            },
        ]
        plan = {
            "schema_version": "query_plan_v1",
            "source_strategy_version": "strategy_v2",
            "dimensions": {"locations": [], "levels": [], "scenarios": []},
            "cell_count": len(cells),
            "cells": cells,
        }
        query_plan = {**plan, "plan_hash": query_builders.query_plan_hash(plan)}
        return {
            "client": "长越科技",
            "job": "机械高级工程师",
            "target_count": 5,
            "workflow_id": "wf-test",
            # These cases exercise the legacy production runner as OpenCLI's explicit fallback.
            "opencli_primary": False,
            "opencli_shadow": False,
            "query_plan_v1": query_plan,
            "query_plan_hash": query_plan["plan_hash"],
            "strategy": {
                "channels": {
                    "liepin": [{"query": "机械 设计"}],
                    "xsaas": [{"query": "机械工程师"}],
                }
            },
        }

    def test_funnel_rows_capture_channel_aggregation(self) -> None:
        self._install_runners(
            liepin_result={
                "ok": True,
                "candidates": 2,
                "a_candidates": 1,
                "b_candidates": 0,
                "detail_capture": {"requested": 2, "complete": 1, "partial": 1, "failed": 0},
                "rounds": [{"name": "画像词 1", "query": "机械 设计", "result_count": 30, "extracted_count": 4, "recommended_count": 2}],
            },
            liepin_candidates=[
                {"channel": "liepin", "name": "张三", "query": "机械 设计", "fit_score": 80},
                {"channel": "liepin", "name": "李四", "query": "机械 设计", "fit_score": 60},
            ],
            liepin_raw_candidates=[
                {"channel": "liepin", "name": "张三", "query": "机械 设计", "fit_score": 80},
                {"channel": "liepin", "name": "李四", "query": "机械 设计", "fit_score": 60},
                {"channel": "liepin", "name": "张三", "query": "机械 设计", "fit_score": 80},
                {"channel": "liepin", "name": "李四", "query": "机械 设计", "fit_score": 60},
            ],
            xsaas_result={
                "ok": True,
                "candidates": 1,
                "rounds": [{"query": "机械工程师", "status": "completed", "result_count": 5, "extracted_count": 3}],
                "detail_capture": {"requested": 1, "complete": 0, "partial": 1, "failed": 0},
            },
            xsaas_candidates=[{"channel": "xsaas", "name": "王五", "query": "机械工程师"}],
            xsaas_raw_candidates=[
                {"channel": "xsaas", "name": "王五", "query": "机械工程师"},
                {"channel": "xsaas", "name": "王五", "query": "机械工程师"},
                {"channel": "xsaas", "name": "王五", "query": "机械工程师"},
            ],
            intake_apply={
                "staged": {
                    "accepted": [
                        {"name": "张三", "channel": "liepin", "source_query": "机械 设计"},
                        {"name": "李四", "channel": "liepin", "source_query": "机械 设计"},
                        {"name": "王五", "channel": "xsaas", "source_query": "机械工程师"},
                    ],
                    "existing": [{"name": "赵六", "channel": "liepin"}],
                    "batch_duplicates": [{"name": "钱七", "channel": "xsaas"}],
                    "errors": [],
                },
                "intake": {
                    "applied": True,
                    "inserted": 3,
                    "receipts": [
                        {"name": "张三", "status": "inserted", "job_candidate_id": 101, "candidate_id": 201},
                        {"name": "李四", "status": "inserted", "job_candidate_id": 102, "candidate_id": 202},
                        {"name": "王五", "status": "inserted", "job_candidate_id": 103, "candidate_id": 203},
                    ],
                },
            },
        )
        result = self.service.capability_runtime.execute_external("multi_channel_sourcing", self._request())
        assert result["sourcing_funnel"]["ok"] is True
        assert result["sourcing_funnel"]["stored"] == 2
        assert result["sourcing_funnel"]["run_id"] == result["run_id"]

        rows = {row["channel"]: row for row in self._funnel_rows()}
        liepin = rows["liepin"]
        assert liepin["run_id"] == result["run_id"]
        assert liepin["workflow_id"] == "wf-test"
        assert liepin["job_id"] == 10
        assert liepin["status"] == "platform_capped"
        assert liepin["query_count"] == 1
        assert json.loads(liepin["queries_json"])[0]["query"] == "机械 设计"
        assert liepin["recall_count"] == 30
        assert liepin["extracted_count"] == 4
        assert liepin["unique_count"] == 2
        assert liepin["dedupe_count"] == 2
        assert (liepin["detail_complete"], liepin["detail_partial"], liepin["detail_failed"]) == (1, 1, 0)
        assert liepin["intake_duplicate_count"] == 1
        assert liepin["intake_new_count"] == 2
        assert liepin["assessed_count"] == 2
        assert liepin["high_score_count"] == 1
        assert liepin["zero_attribution"] is None

        xsaas = rows["xsaas"]
        assert xsaas["recall_count"] == 5
        assert xsaas["extracted_count"] == 3
        assert xsaas["unique_count"] == 1
        assert xsaas["dedupe_count"] == 2
        assert xsaas["intake_duplicate_count"] == 1
        assert xsaas["intake_new_count"] == 1
        assert xsaas["assessed_count"] == 0
        assert xsaas["zero_attribution"] is None

        # 有候选的渠道不打 0 结果标记
        for run in result["channel_runs"]:
            assert "zero_attribution" not in run
        AgentService.validate_external_result("multi_channel_sourcing", result)
        for run in result["channel_runs"]:
            assert "quality" not in run

    def test_raw_recall_ledger_retains_low_score_and_existing_candidates(self) -> None:
        request = self._request()
        raw_candidates = {
            "liepin": [
                {
                    "channel": "liepin", "source_query": "机械 设计", "res_id_encode": "lp-accepted",
                    "name": "张三", "company": "甲公司", "title": "机械工程师", "fit_score": 82,
                },
                {
                    "channel": "liepin", "source_query": "机械 设计", "res_id_encode": "lp-existing",
                    "name": "李四", "company": "乙公司", "title": "结构工程师", "fit_score": 70,
                },
                {
                    "channel": "liepin", "source_query": "机械 设计", "res_id_encode": "lp-low",
                    "name": "王五", "company": "丙公司", "title": "工艺工程师", "fit_score": 41,
                },
            ],
            "xsaas": [],
        }
        applied = {
            "staged": {
                "accepted": [{"channel": "liepin", "source_query": "机械 设计", "name": "张三", "company": "甲公司", "title": "机械工程师"}],
                "existing": [{"channel": "liepin", "source_query": "机械 设计", "name": "李四", "company": "乙公司", "title": "结构工程师"}],
                "batch_duplicates": [],
                "errors": [],
            },
            "intake": {
                "receipts": [{"name": "张三", "status": "inserted", "candidate_id": 201, "job_candidate_id": 101}],
            },
        }

        result = self.service.capability_runtime._persist_candidate_recalls(
            run_id="asa-source-ledger",
            workflow_id="wf-ledger",
            client="长越科技",
            job="机械高级工程师",
            query_plan=request["query_plan_v1"],
            raw_candidates=raw_candidates,
            applied=applied,
            min_score=55,
        )

        assert result["stored"] == 3
        conn = self.service._connect()
        try:
            rows = conn.execute(
                "SELECT source_candidate_id,duplicate_state,exclusion_reason,candidate_id,job_candidate_id "
                "FROM agent_candidate_recalls WHERE run_id=? ORDER BY source_candidate_id",
                ("asa-source-ledger",),
            ).fetchall()
        finally:
            conn.close()
        by_id = {row["source_candidate_id"]: dict(row) for row in rows}
        assert by_id["lp-accepted"]["duplicate_state"] == "accepted"
        assert by_id["lp-accepted"]["candidate_id"] == 201
        assert by_id["lp-accepted"]["job_candidate_id"] == 101
        assert by_id["lp-existing"]["duplicate_state"] == "existing"
        assert by_id["lp-low"]["duplicate_state"] == "not_intaked"
        assert by_id["lp-low"]["exclusion_reason"] == "score_below_threshold"

    def test_recall_receipts_are_bound_by_identity_not_name_only(self) -> None:
        request = self._request()
        raw_candidates = {
            "liepin": [
                {
                    "channel": "liepin", "source_query": "机械 设计", "res_id_encode": "lp-a",
                    "name": "王某", "company": "甲公司", "title": "机械工程师", "fit_score": 82,
                },
                {
                    "channel": "liepin", "source_query": "机械 设计", "res_id_encode": "lp-b",
                    "name": "王某", "company": "乙公司", "title": "结构工程师", "fit_score": 79,
                },
            ],
            "xsaas": [],
        }
        accepted = [
            {"channel": "liepin", "source_query": "机械 设计", "name": "王某", "company": "甲公司", "title": "机械工程师"},
            {"channel": "liepin", "source_query": "机械 设计", "name": "王某", "company": "乙公司", "title": "结构工程师"},
        ]
        applied = {
            "staged": {"accepted": accepted, "existing": [], "batch_duplicates": [], "errors": []},
            "intake": {"receipts": [
                {"name": "王某", "candidate_id": 201, "job_candidate_id": 101},
                {"name": "王某", "candidate_id": 202, "job_candidate_id": 102},
            ]},
        }

        self.service.capability_runtime._persist_candidate_recalls(
            run_id="asa-source-same-name",
            workflow_id="wf-same-name",
            client="长越科技",
            job="机械高级工程师",
            query_plan=request["query_plan_v1"],
            raw_candidates=raw_candidates,
            applied=applied,
            min_score=55,
        )

        conn = self.service._connect()
        try:
            rows = conn.execute(
                "SELECT source_candidate_id,candidate_id,job_candidate_id FROM agent_candidate_recalls "
                "WHERE run_id=? ORDER BY source_candidate_id",
                ("asa-source-same-name",),
            ).fetchall()
        finally:
            conn.close()
        assert [tuple(row) for row in rows] == [("lp-a", 201, 101), ("lp-b", 202, 102)]

    def test_execute_external_persists_raw_rows_before_score_gate(self) -> None:
        accepted = {
            "channel": "liepin", "source_query": "机械 设计", "res_id_encode": "lp-good",
            "name": "张三", "company": "甲公司", "title": "机械工程师", "fit_score": 82,
        }
        low_score = {
            "channel": "liepin", "source_query": "机械 设计", "res_id_encode": "lp-low",
            "name": "李四", "company": "乙公司", "title": "工艺工程师", "fit_score": 38,
        }
        self._install_runners(
            liepin_result={"ok": True, "candidates": 1, "rounds": [{"query": "机械 设计", "result_count": 2, "extracted_count": 2}]},
            liepin_candidates=[accepted],
            liepin_raw_candidates=[accepted, low_score],
            xsaas_result={"ok": True, "candidates": 0, "rounds": [{"query": "机械工程师", "result_count": 0, "extracted_count": 0}]},
            xsaas_candidates=[],
            xsaas_raw_candidates=[],
            intake_apply={
                "staged": {"accepted": [accepted], "existing": [], "batch_duplicates": [], "errors": []},
                "intake": {"inserted": 1, "receipts": [{"name": "张三", "status": "inserted", "candidate_id": 201, "job_candidate_id": 101}]},
            },
        )

        result = self.service.capability_runtime.execute_external("multi_channel_sourcing", self._request())

        assert result["candidate_recall_ledger"]["stored"] == 2
        assert result["query_cell_states"]["terminal_counts"] == {"exhausted": 2}
        certificate = result["coverage_certificate"]
        assert certificate["schema_version"] == "coverage_certificate_v1"
        assert certificate["coverage_status"] == "approved_query_cells_exhausted"
        assert certificate["query_cells"] == {
            "approved": 2, "executed": 2, "exhausted": 2,
            "platform_capped": 0, "blocked": 0, "failed": 0, "pending": 0,
        }
        assert certificate["candidate_recall"]["raw_occurrences"] == 2
        assert certificate["candidate_recall"]["unique_identities"] == 2
        assert certificate["claims"]["all_candidates_covered"] is False
        assert certificate["claims"]["defensible_claim"] == (
            "已穷尽批准的渠道关键词查询单元；地点、职级、场景未作为平台筛选执行"
        )
        conn = self.service._connect()
        try:
            low = conn.execute(
                "SELECT fit_score,duplicate_state,exclusion_reason FROM agent_candidate_recalls "
                "WHERE run_id=? AND source_candidate_id='lp-low'",
                (result["run_id"],),
            ).fetchone()
            stored_certificate = conn.execute(
                "SELECT certificate_json FROM agent_sourcing_coverage_certificates WHERE run_id=?",
                (result["run_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert dict(low) == {
            "fit_score": 38,
            "duplicate_state": "not_intaked",
            "exclusion_reason": "score_below_threshold",
        }
        assert json.loads(stored_certificate["certificate_json"])["certificate_id"] == certificate["certificate_id"]

    def test_execute_external_persists_raw_evidence_before_apply_failure(self) -> None:
        raw = {
            "channel": "liepin", "source_query": "机械 设计", "res_id_encode": "lp-before-apply",
            "name": "张三", "company": "甲公司", "title": "机械工程师", "fit_score": 82,
        }
        self._install_runners(
            liepin_result={
                "ok": True,
                "candidates": 1,
                "rounds": [{
                    "query": "机械 设计", "result_count": 1, "extracted_count": 1,
                    "terminal_state": "exhausted", "terminal_reason": "reported_total_exhausted",
                }],
            },
            liepin_candidates=[raw],
            liepin_raw_candidates=[raw],
            xsaas_result={
                "ok": True,
                "candidates": 0,
                "rounds": [{
                    "query": "机械工程师", "result_count": 0, "extracted_count": 0,
                    "terminal_state": "exhausted", "terminal_reason": "reported_total_exhausted",
                }],
            },
            xsaas_candidates=[],
            xsaas_raw_candidates=[],
            intake_apply=RuntimeError("simulated intake apply failure"),
        )

        with self.assertRaisesRegex(RuntimeError, "simulated intake apply failure"):
            self.service.capability_runtime.execute_external("multi_channel_sourcing", self._request())

        conn = self.service._connect()
        try:
            recall = conn.execute(
                "SELECT source_candidate_id,duplicate_state FROM agent_candidate_recalls "
                "WHERE source_candidate_id='lp-before-apply'",
            ).fetchone()
            cells = conn.execute(
                "SELECT status,COUNT(*) AS count FROM agent_sourcing_query_cells GROUP BY status ORDER BY status",
            ).fetchall()
        finally:
            conn.close()
        assert tuple(recall) == ("lp-before-apply", "not_intaked")
        assert [tuple(row) for row in cells] == [("exhausted", 2)]

    def test_query_cell_states_use_explicit_terminal_reasons_and_cursor(self) -> None:
        request = self._request()
        result = self.service.capability_runtime._persist_query_cell_states(
            run_id="asa-source-cells",
            workflow_id="wf-cells",
            client="长越科技",
            job="机械高级工程师",
            query_plan=request["query_plan_v1"],
            channel_runs=[
                {
                    "channel": "liepin", "status": "completed",
                    "result": {"rounds": [{
                        "query": "机械 设计", "status": "completed", "result_count": 80,
                        "extracted_count": 24, "pages_fetched": 1, "cursor": {"page": 2},
                    }]},
                },
                {
                    "channel": "xsaas", "status": "completed",
                    "result": {"rounds": [{
                        "query": "机械工程师", "status": "completed", "result_count": 3,
                        "extracted_count": 3, "pages_fetched": 2, "cursor": None,
                    }]},
                },
            ],
        )

        assert result["terminal_counts"] == {"exhausted": 1, "platform_capped": 1}
        conn = self.service._connect()
        try:
            rows = conn.execute(
                "SELECT channel,status,reported_total,extracted_count,pages_fetched,cursor_json,terminal_reason "
                "FROM agent_sourcing_query_cells WHERE run_id=? ORDER BY channel",
                ("asa-source-cells",),
            ).fetchall()
        finally:
            conn.close()
        by_channel = {row["channel"]: dict(row) for row in rows}
        assert by_channel["liepin"]["status"] == "platform_capped"
        assert by_channel["liepin"]["terminal_reason"] == "reported_total_not_exhausted"
        assert json.loads(by_channel["liepin"]["cursor_json"]) == {"page": 2}
        assert by_channel["xsaas"]["status"] == "exhausted"
        assert by_channel["xsaas"]["pages_fetched"] == 2

        continuation = self.service.capability_runtime._sourcing_continuation(
            request=request,
            run_id="asa-source-cells",
            query_plan=request["query_plan_v1"],
        )
        assert continuation["summary"]["scheduled"] is True
        assert continuation["summary"]["remaining_cells"] == 1
        assert continuation["request"]["resume_run_id"] == "asa-source-cells"
        assert continuation["request"]["query_plan_hash"] == request["query_plan_hash"]

    def test_execute_external_returns_cursor_continuation_request(self) -> None:
        request = self._request()
        request["max_pages_per_query"] = 1
        liepin_raw = [{
            "channel": "liepin", "source_query": "机械 设计", "res_id_encode": "lp-page-1",
            "name": "张三", "current_company": "甲公司", "current_title": "机械工程师",
            "page_number": 1, "position_index": 1, "fit_score": 70,
        }]
        self._install_runners(
            liepin_result={
                "ok": True,
                "candidates": 1,
                "detail_capture": {"requested": 1, "complete": 0, "partial": 1, "failed": 0},
                "rounds": [{
                    "query": "机械 设计", "result_count": 3, "extracted_count": 1,
                    "unique_count": 1, "pages_fetched": 1, "terminal_state": "platform_capped",
                    "terminal_reason": "page_safety_limit", "cursor": {"page": 2},
                }],
            },
            liepin_candidates=liepin_raw,
            liepin_raw_candidates=liepin_raw,
            xsaas_result={
                "ok": True,
                "candidates": 0,
                "detail_capture": {"requested": 0, "complete": 0, "partial": 0, "failed": 0},
                "rounds": [{
                    "query": "机械工程师", "result_count": 0, "extracted_count": 0,
                    "unique_count": 0, "pages_fetched": 1, "terminal_state": "exhausted",
                    "terminal_reason": "reported_total_exhausted", "cursor": None,
                }],
            },
        )

        result = self.service.capability_runtime.execute_external("multi_channel_sourcing", request)

        self.assertEqual(result["coverage_certificate"]["coverage_status"], "platform_truncated")
        self.assertTrue(result["continuation"]["scheduled"])
        self.assertEqual(result["continuation"]["remaining_cells"], 1)
        continuation = result["_continuation_request"]
        self.assertEqual(continuation["resume_run_id"], result["run_id"])
        self.assertEqual(continuation["_continuation_index"], 1)
        self.assertEqual(continuation["query_plan_hash"], request["query_plan_hash"])
        runnable = self.service.capability_runtime._resume_query_cells(
            result["run_id"], request["query_plan_v1"]
        )
        self.assertEqual([item["cell_id"] for item in runnable], ["qpc_test_liepin"])
        self.assertEqual(runnable[0]["execution_cursor"], {"page": 2})

    def test_initial_execution_batches_eight_cells_and_leaves_rest_pending(self) -> None:
        request = self._request()
        cells = [
            {
                "cell_id": f"qpc_batch_liepin_{index}",
                "channel": "liepin",
                "query": f"机械 设计{index}",
                "locations": [], "levels": [], "scenarios": [], "priority": index,
                "provenance": [{"kind": "keyword_group", "group": f"mechanical-{index}"}],
            }
            for index in range(1, 10)
        ]
        cells.append({
            "cell_id": "qpc_batch_xsaas_10",
            "channel": "xsaas",
            "query": "机械工程师10",
            "locations": [], "levels": [], "scenarios": [], "priority": 10,
            "provenance": [{"kind": "keyword_group", "group": "mechanical-10"}],
        })
        base = {
            "schema_version": "query_plan_v1",
            "source_strategy_version": "strategy_v2",
            "dimensions": {"locations": [], "levels": [], "scenarios": []},
            "cell_count": len(cells),
            "cells": cells,
        }
        plan = {**base, "plan_hash": query_builders.query_plan_hash(base)}
        request.update({"query_plan_v1": plan, "query_plan_hash": plan["plan_hash"]})
        self._install_runners(
            liepin_result={
                "ok": True,
                "candidates": 0,
                "rounds": [
                    {
                        "query": cell["query"], "result_count": 0, "extracted_count": 0,
                        "unique_count": 0, "pages_fetched": 1, "terminal_state": "exhausted",
                        "terminal_reason": "reported_total_exhausted", "cursor": None,
                    }
                    for cell in cells[:8]
                ],
            },
            liepin_candidates=[],
            liepin_raw_candidates=[],
            xsaas_result={"ok": True, "candidates": 0, "rounds": []},
            xsaas_candidates=[],
            xsaas_raw_candidates=[],
        )

        result = self.service.capability_runtime.execute_external("multi_channel_sourcing", request)

        certificate = result["coverage_certificate"]
        self.assertEqual(certificate["query_cells"]["exhausted"], 8)
        self.assertEqual(certificate["query_cells"]["pending"], 2)
        self.assertEqual(certificate["coverage_status"], "coverage_unknown")
        self.assertEqual(result["continuation"]["remaining_cells"], 2)
        self.assertTrue(result["continuation"]["scheduled"])
        self.assertEqual(result["_continuation_request"]["resume_run_id"], result["run_id"])

    def test_opencli_partial_recall_only_paginates_unfinished_query_cells(self) -> None:
        runtime = self.service.capability_runtime
        commands: list[list[str]] = []

        def write_rows(command: list[str], flag: str, rows: list[dict]) -> None:
            path = Path(command[command.index(flag) + 1])
            path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

        def fake_run_json(command: list[str], timeout: int = 300) -> dict:
            cmd = [str(part) for part in command]
            commands.append(cmd)
            if "--mode" in cmd and cmd[cmd.index("--mode") + 1] == "primary":
                channel = cmd[cmd.index("--channel") + 1]
                if channel == "liepin":
                    page_one = [{
                        "channel": "liepin", "query": "机械 设计", "candidate_id": "lp-1",
                        "name": "甲", "company": "A", "title": "机械工程师",
                        "page_number": 1, "position_index": 1, "fit_score": 70,
                    }]
                    write_rows(cmd, "--output", page_one)
                    write_rows(cmd, "--raw-output", page_one)
                    return {
                        "ok": False, "coverage_complete": False, "intake_ready": True,
                        "mode": "opencli_primary_recall", "channel": "liepin",
                        "rounds": [{
                            "query": "机械 设计", "result_count": 2, "extracted_count": 1,
                            "unique_count": 1, "pages_fetched": 1, "terminal_state": "platform_capped",
                            "terminal_reason": "opencli_limit_below_reported_total", "cursor": {"page": 2},
                        }],
                    }
                write_rows(cmd, "--output", [])
                write_rows(cmd, "--raw-output", [])
                return {
                    "ok": False, "coverage_complete": True, "intake_ready": False,
                    "mode": "opencli_primary_recall", "channel": "xsaas",
                    "rounds": [{
                        "query": "机械工程师", "result_count": 0, "extracted_count": 0,
                        "unique_count": 0, "pages_fetched": 1, "terminal_state": "exhausted",
                        "terminal_reason": "reported_total_exhausted", "cursor": None,
                    }],
                }
            if "--json-output" in cmd:
                queries_path = Path(cmd[cmd.index("--queries-json") + 1])
                fallback_entries = json.loads(queries_path.read_text(encoding="utf-8"))["queries"]
                self.assertEqual(fallback_entries, [{
                    "cell_id": "qpc_test_liepin", "query": "机械 设计",
                    "evaluation_constraints": {"locations": [], "levels": [], "scenarios": []},
                    "execution_filters": {}, "cursor": {"page": 2}, "collected_before": 1,
                    "seen_candidate_keys": ["lp-1"],
                }])
                page_two = [{
                    "channel": "liepin", "source_query": "机械 设计", "res_id_encode": "lp-2",
                    "name": "乙", "current_company": "B", "current_title": "机械工程师",
                    "page_number": 2, "position_index": 1, "fit_score": 72,
                }]
                write_rows(cmd, "--json-output", page_two)
                write_rows(cmd, "--raw-json-output", page_two)
                return {
                    "ok": True, "candidates": 1,
                    "detail_capture": {"requested": 1, "complete": 1, "partial": 0, "failed": 0},
                    "rounds": [{
                        "query": "机械 设计", "result_count": 2, "extracted_count": 1,
                        "unique_count": 1, "pages_fetched": 1, "terminal_state": "exhausted",
                        "terminal_reason": "reported_total_exhausted", "cursor": None,
                    }],
                }
            if "intake" in cmd:
                if "--apply" in cmd:
                    return {"staged": {"accepted": []}, "intake": {"inserted": 0, "receipts": []}}
                return {"staged": {"accepted_count": 0}, "intake": {"applied": False, "inserted": 0}}
            raise AssertionError(f"unexpected command: {cmd}")

        runtime._run_json = fake_run_json  # type: ignore[method-assign]
        runtime._run = lambda command, timeout=300: subprocess.CompletedProcess(  # type: ignore[method-assign]
            command, 0, stdout="sync ok", stderr="",
        )
        request = self._request()
        request.update({"opencli_primary": True, "opencli_shadow": True})

        result = runtime.execute_external("multi_channel_sourcing", request)

        by_channel = {item["channel"]: item for item in result["channel_runs"]}
        self.assertEqual(by_channel["liepin"]["recall_engine"], "opencli_paginated")
        self.assertEqual(by_channel["xsaas"]["recall_engine"], "opencli")
        self.assertEqual(
            by_channel["liepin"]["result"]["rounds"][0]["extracted_count"], 2,
        )
        self.assertTrue(result["coverage_certificate"]["evidence_integrity"]["passed"])
        self.assertEqual(
            result["coverage_certificate"]["coverage_status"],
            "approved_query_cells_exhausted",
        )
        self.assertNotIn("_continuation_request", result)
        production_calls = [cmd for cmd in commands if "--json-output" in cmd]
        self.assertEqual(len(production_calls), 1)
        self.assertFalse(any("xsaas_candidate_search.py" in " ".join(cmd) for cmd in commands))
        self.assertTrue(all(
            channel["reason"] == "recall_engine_opencli"
            for channel in result["opencli_shadow"]["channels"]
        ))

    def test_liepin_pagination_failure_keeps_page_one_ledger_and_cursor(self) -> None:
        runtime = self.service.capability_runtime

        def write_rows(command: list[str], flag: str, rows: list[dict]) -> None:
            Path(command[command.index(flag) + 1]).write_text(
                json.dumps(rows, ensure_ascii=False), encoding="utf-8",
            )

        def fake_run_json(command: list[str], timeout: int = 300) -> dict:
            cmd = [str(part) for part in command]
            if "--mode" in cmd and cmd[cmd.index("--mode") + 1] == "primary":
                channel = cmd[cmd.index("--channel") + 1]
                if channel == "liepin":
                    page_one = [{
                        "channel": "liepin", "query": "机械 设计", "candidate_id": "lp-page-one",
                        "name": "甲", "company": "A", "title": "机械工程师",
                        "page_number": 1, "position_index": 1, "fit_score": 70,
                    }]
                    write_rows(cmd, "--output", page_one)
                    write_rows(cmd, "--raw-output", page_one)
                    return {
                        "ok": False, "coverage_complete": False, "intake_ready": True,
                        "mode": "opencli_primary_recall", "channel": "liepin",
                        "rounds": [{
                            "query": "机械 设计", "result_count": 3, "extracted_count": 1,
                            "unique_count": 1, "pages_fetched": 1,
                            "terminal_state": "platform_capped",
                            "terminal_reason": "opencli_limit_below_reported_total",
                            "cursor": {"page": 2},
                        }],
                    }
                write_rows(cmd, "--output", [])
                write_rows(cmd, "--raw-output", [])
                return {
                    "ok": False, "coverage_complete": True, "intake_ready": False,
                    "mode": "opencli_primary_recall", "channel": "xsaas",
                    "rounds": [{
                        "query": "机械工程师", "result_count": 0, "extracted_count": 0,
                        "unique_count": 0, "pages_fetched": 1,
                        "terminal_state": "exhausted",
                        "terminal_reason": "reported_total_exhausted", "cursor": None,
                    }],
                }
            if "--json-output" in cmd:
                raise RuntimeError("pagination transport failed")
            if "intake" in cmd:
                if "--apply" in cmd:
                    return {"staged": {"accepted": []}, "intake": {"inserted": 0, "receipts": []}}
                return {"staged": {"accepted_count": 0}, "intake": {"applied": False, "inserted": 0}}
            raise AssertionError(f"unexpected command: {cmd}")

        runtime._run_json = fake_run_json  # type: ignore[method-assign]
        runtime._run = lambda command, timeout=300: subprocess.CompletedProcess(  # type: ignore[method-assign]
            command, 0, stdout="sync ok", stderr="",
        )
        request = self._request()
        request.update({"opencli_primary": True, "opencli_shadow": True})

        result = runtime.execute_external("multi_channel_sourcing", request)

        self.assertEqual(result["coverage_certificate"]["coverage_status"], "platform_truncated")
        self.assertTrue(result["continuation"]["scheduled"])
        conn = self.service._connect()
        try:
            recall = conn.execute(
                "SELECT source_candidate_id,query_cell_id FROM agent_candidate_recalls "
                "WHERE run_id=? AND source_candidate_id='lp-page-one'",
                (result["run_id"],),
            ).fetchone()
            cell = conn.execute(
                "SELECT status,cursor_json,terminal_reason FROM agent_sourcing_query_cells "
                "WHERE run_id=? AND cell_id='qpc_test_liepin'",
                (result["run_id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(tuple(recall), ("lp-page-one", "qpc_test_liepin"))
        self.assertEqual(cell["status"], "platform_capped")
        self.assertEqual(json.loads(cell["cursor_json"]), {"page": 2})
        self.assertEqual(cell["terminal_reason"], "opencli_limit_below_reported_total")

    def test_resume_selects_only_retryable_cells_from_same_plan(self) -> None:
        query_plan = self._request()["query_plan_v1"]
        conn = self.service._connect()
        try:
            for cell, status, retries in zip(
                query_plan["cells"], ("failed", "exhausted"), (1, 0), strict=True,
            ):
                conn.execute(
                    """
                    INSERT INTO agent_sourcing_query_cells
                    (run_id,workflow_id,job_id,plan_hash,cell_id,channel,query,priority,status,retry_count)
                    VALUES ('asa-source-resume','wf-resume',10,?,?,?,?,?,?,?)
                    """,
                    (
                        query_plan["plan_hash"], cell["cell_id"], cell["channel"], cell["query"],
                        cell["priority"], status, retries,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        runnable = self.service.capability_runtime._resume_query_cells(
            "asa-source-resume", query_plan, max_retries=3,
        )

        assert [cell["channel"] for cell in runnable] == ["liepin"]
        assert runnable[0]["query"] == "机械 设计"

    def test_resume_selects_platform_cap_only_when_cursor_is_available(self) -> None:
        query_plan = self._request()["query_plan_v1"]
        conn = self.service._connect()
        try:
            for cell, cursor in zip(
                query_plan["cells"], ({"page": 51}, {}), strict=True,
            ):
                conn.execute(
                    """
                    INSERT INTO agent_sourcing_query_cells
                    (run_id,workflow_id,job_id,plan_hash,cell_id,channel,query,priority,status,cursor_json,terminal_reason)
                    VALUES ('asa-source-cursor','wf-resume',10,?,?,?,?,?,'platform_capped',?,'page_safety_limit')
                    """,
                    (
                        query_plan["plan_hash"], cell["cell_id"], cell["channel"], cell["query"],
                        cell["priority"], json.dumps(cursor),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        runnable = self.service.capability_runtime._resume_query_cells(
            "asa-source-cursor", query_plan, max_retries=3,
        )

        assert len(runnable) == 1
        assert runnable[0]["channel"] == "liepin"
        assert runnable[0]["execution_cursor"] == {"page": 51}

    def test_certificate_downgrades_exhausted_grid_when_recall_ledger_is_incomplete(self) -> None:
        request = self._request()
        self.service.capability_runtime._persist_query_cell_states(
            run_id="asa-source-ledger-gap",
            workflow_id="wf-ledger-gap",
            client="长越科技",
            job="机械高级工程师",
            query_plan=request["query_plan_v1"],
            channel_runs=[
                {
                    "channel": "liepin", "status": "completed",
                    "result": {"rounds": [{
                        "query": "机械 设计", "result_count": 2, "extracted_count": 2,
                        "terminal_state": "exhausted", "terminal_reason": "reported_total_exhausted",
                    }]},
                },
                {
                    "channel": "xsaas", "status": "completed",
                    "result": {"rounds": [{
                        "query": "机械工程师", "result_count": 0, "extracted_count": 0,
                        "terminal_state": "exhausted", "terminal_reason": "reported_total_exhausted",
                    }]},
                },
            ],
        )

        certificate = self.service.capability_runtime._build_coverage_certificate(
            run_id="asa-source-ledger-gap",
            workflow_id="wf-ledger-gap",
            client="长越科技",
            job="机械高级工程师",
            query_plan=request["query_plan_v1"],
        )

        assert certificate["query_cells"]["exhausted"] == 2
        assert certificate["evidence_integrity"]["passed"] is False
        assert certificate["evidence_integrity"]["expected_extracted_occurrences"] == 2
        assert certificate["evidence_integrity"]["mapped_recall_occurrences"] == 0
        assert certificate["coverage_status"] == "coverage_unknown"
        assert "recall_ledger_mismatch" in certificate["claims"]["coverage_unknown_reasons"]

    def test_zero_result_attribution_flows_to_funnel_and_quality(self) -> None:
        self._install_runners(
            liepin_result={
                "ok": True,
                "candidates": 0,
                "detail_capture": {"requested": 0, "complete": 0, "partial": 0, "failed": 0},
                "rounds": [{"name": "画像词 1", "query": "机械 设计", "result_count": 0, "extracted_count": 0, "recommended_count": 0}],
            },
            xsaas_result=RuntimeError("X-SAAS_LOGIN_REQUIRED: 未找到已登录的 X-SaaS 页面"),
        )
        result = self.service.capability_runtime.execute_external("multi_channel_sourcing", self._request())

        rows = {row["channel"]: row for row in self._funnel_rows()}
        assert rows["liepin"]["status"] == "completed"
        assert rows["liepin"]["unique_count"] == 0
        assert rows["liepin"]["zero_attribution"] == "no_results"
        assert rows["xsaas"]["status"] == "blocked"
        assert rows["xsaas"]["zero_attribution"] == "session_expired"
        assert "X-SAAS_LOGIN_REQUIRED" in rows["xsaas"]["error"]

        channel_runs = {run["channel"]: run for run in result["channel_runs"]}
        assert channel_runs["liepin"]["zero_attribution"] == "no_results"
        assert channel_runs["xsaas"]["zero_attribution"] == "session_expired"

        AgentService.validate_external_result("multi_channel_sourcing", result)
        assert channel_runs["liepin"]["quality"] == "zero_attributed"
        assert "quality" not in channel_runs["xsaas"]  # blocked 渠道已有 status 区分，不打 quality

    def test_pool_saturated_attribution_flows_to_funnel(self) -> None:
        # S4-3c-1：召回正常但抓取全部被排重（dedupe_rate 100% > 90%）→ pool_saturated 落漏斗
        self._install_runners(
            liepin_result={
                "ok": True,
                "candidates": 0,
                "detail_capture": {"requested": 0, "complete": 0, "partial": 0, "failed": 0},
                "rounds": [{"name": "画像词 1", "query": "机械 设计", "result_count": 120, "extracted_count": 120, "recommended_count": 0}],
            },
            xsaas_result={"ok": True, "candidates": 0, "rounds": []},
        )
        result = self.service.capability_runtime.execute_external("multi_channel_sourcing", self._request())

        rows = {row["channel"]: row for row in self._funnel_rows()}
        assert rows["liepin"]["unique_count"] == 0
        assert rows["liepin"]["dedupe_count"] == 120
        assert rows["liepin"]["zero_attribution"] == "pool_saturated"
        assert rows["xsaas"]["zero_attribution"] == "unknown"

        channel_runs = {run["channel"]: run for run in result["channel_runs"]}
        assert channel_runs["liepin"]["zero_attribution"] == "pool_saturated"
        AgentService.validate_external_result("multi_channel_sourcing", result)
        assert channel_runs["liepin"]["quality"] == "zero_attributed"

    def test_liepin_failure_is_checkpointed_without_losing_other_channel(self) -> None:
        self._install_runners(
            liepin_result=RuntimeError("猎聘登录已过期，请在 Chrome 里登录猎聘后再继续。"),
            xsaas_result={"ok": True, "candidates": 0, "rounds": [{
                "query": "机械工程师", "result_count": 0, "extracted_count": 0,
                "terminal_state": "exhausted", "terminal_reason": "reported_total_exhausted",
            }]},
        )
        result = self.service.capability_runtime.execute_external("multi_channel_sourcing", self._request())
        rows = {row["channel"]: row for row in self._funnel_rows()}
        assert set(rows) == {"liepin", "xsaas"}
        assert rows["liepin"]["status"] == "blocked"
        assert rows["liepin"]["zero_attribution"] == "session_expired"
        assert "登录已过期" in rows["liepin"]["error"]
        assert result["continuation"]["scheduled"] is True

    def test_long_traceback_keeps_tail_so_login_signal_survives(self) -> None:
        # T3 实战回归：traceback 超 1000 字符时头部截断会丢掉末尾异常行（归因信号），
        # X-SAAS_LOGIN_REQUIRED 会被系统性误判为 unknown；_trim_error 尾部保留修复。
        traceback_text = (
            "Traceback (most recent call last):\n"
            + "".join(f'  File "/x/frame_{i}.py", line {i}, in frame\n    call_{i}()\n' for i in range(40))
            + '  File "/x/xsaas_candidate_search.py", line 50, in choose_authenticated_tab\n'
            '    raise RuntimeError("X-SAAS_LOGIN_REQUIRED: 未找到已登录的 X-SaaS 页面")\n'
            "RuntimeError: X-SAAS_LOGIN_REQUIRED: 未找到已登录的 X-SaaS 页面"
        )
        assert len(traceback_text) > 1000
        self._install_runners(
            liepin_result={"ok": True, "candidates": [], "rounds": []},
            xsaas_result=RuntimeError(traceback_text),
        )
        self.service.capability_runtime.execute_external("multi_channel_sourcing", self._request())
        rows = self._funnel_rows()
        xsaas = next(row for row in rows if row["channel"] == "xsaas")
        assert xsaas["status"] == "blocked"
        assert xsaas["zero_attribution"] == "session_expired"
        assert "X-SAAS_LOGIN_REQUIRED" in xsaas["error"]

    def test_funnel_upsert_keeps_one_row_per_run_channel(self) -> None:
        runtime = self.service.capability_runtime
        base = dict(run_id="run-fixed", workflow_id="wf-test", client="长越科技", job="机械高级工程师")
        for recall in (10, 25):
            runtime._persist_sourcing_funnel(
                **base,
                channel_runs=[{"channel": "liepin", "status": "completed", "result": {
                    "ok": True, "candidates": 1,
                    "rounds": [{"query": "q", "result_count": recall, "extracted_count": 1}],
                    "detail_capture": {"complete": 1, "partial": 0, "failed": 0},
                }}],
                channel_candidates={"liepin": [{"name": "张三", "fit_score": 70}]},
                applied={"staged": {}, "intake": {}},
                attributions={"channel_new": {"liepin": 1}},
            )
        rows = self._funnel_rows()
        assert len(rows) == 1
        assert rows[0]["recall_count"] == 25


class SourcingFunnelApiTest(unittest.TestCase):
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
        conn.commit()
        conn.close()

    def _insert_run(
        self,
        db_path: Path,
        *,
        run_id: str,
        workflow_id: str,
        channel: str,
        status: str = "completed",
        recall: int = 0,
        extracted: int = 0,
        unique: int = 0,
        detail: tuple[int, int, int] = (0, 0, 0),
        intake_new: int = 0,
        zero_attribution: str | None = None,
        queries: list[dict] | None = None,
        created_at: str = "2026-07-22 10:00:00",
    ) -> None:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            INSERT INTO agent_sourcing_funnel
            (run_id,workflow_id,job_id,client,job,channel,status,query_count,queries_json,
             recall_count,extracted_count,dedupe_count,unique_count,
             detail_complete,detail_partial,detail_failed,intake_new_count,zero_attribution,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id, workflow_id, 10, "长越科技", "机械高级工程师", channel, status,
                len(queries or []), json.dumps(queries or [], ensure_ascii=False),
                recall, extracted, max(0, extracted - unique), unique,
                detail[0], detail[1], detail[2], intake_new, zero_attribution, created_at,
            ),
        )
        conn.commit()
        conn.close()

    def test_empty_structure_when_no_sourcing_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "asa.db"
            self._create_db(db_path)
            with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
                response = client.get("/api/v1/workflows/wf-missing/sourcing-funnel")
                assert response.status_code == 200
                body = response.json()
                assert body["ok"] is True
                assert body["workflow_id"] == "wf-missing"
                assert body["channels"] == []
                assert body["runs"] == []

    def test_funnel_api_aggregates_channels_and_complete_rate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "asa.db"
            self._create_db(db_path)
            app = create_app(db_path=db_path, start_legacy=False)
            self._insert_run(
                db_path, run_id="run-1", workflow_id="wf-1", channel="liepin",
                recall=30, extracted=4, unique=2, detail=(1, 1, 0), intake_new=2,
                queries=[{"query": "机械 设计", "result_count": 30, "extracted_count": 4}],
                created_at="2026-07-22 10:00:00",
            )
            self._insert_run(
                db_path, run_id="run-2", workflow_id="wf-1", channel="liepin",
                recall=10, extracted=2, unique=1, detail=(1, 0, 0), intake_new=1,
                created_at="2026-07-22 11:00:00",
            )
            self._insert_run(
                db_path, run_id="run-1", workflow_id="wf-1", channel="xsaas",
                status="blocked", zero_attribution="session_expired",
                created_at="2026-07-22 10:05:00",
            )
            self._insert_run(
                db_path, run_id="run-9", workflow_id="wf-other", channel="liepin",
                recall=99, extracted=9, unique=9, detail=(9, 0, 0), intake_new=9,
            )
            with TestClient(app) as client:
                response = client.get("/api/v1/workflows/wf-1/sourcing-funnel")
                assert response.status_code == 200
                body = response.json()

        assert body["ok"] is True
        assert body["workflow_id"] == "wf-1"
        channels = {item["channel"]: item for item in body["channels"]}
        assert set(channels) == {"liepin", "xsaas"}

        liepin = channels["liepin"]
        assert liepin["runs"] == 2
        assert liepin["recall_count"] == 40
        assert liepin["extracted_count"] == 6
        assert liepin["unique_count"] == 3
        assert liepin["intake_new_count"] == 3
        assert liepin["detail"] == {"complete": 2, "partial": 1, "failed": 0, "complete_rate": round(2 / 3, 4)}
        assert liepin["zero_attribution"] is None

        xsaas = channels["xsaas"]
        assert xsaas["status"] == "blocked"
        assert xsaas["zero_attribution"] == "session_expired"
        # 分母为 0 时 complete_rate 为 null
        assert xsaas["detail"] == {"complete": 0, "partial": 0, "failed": 0, "complete_rate": None}

        runs = body["runs"]
        assert len(runs) == 3
        assert all(run["run_id"] != "run-9" for run in runs)
        latest = runs[0]
        assert latest["run_id"] == "run-2"
        assert latest["channel"] == "liepin"
        assert latest["detail"]["complete_rate"] == 1.0
        run1_liepin = next(run for run in runs if run["run_id"] == "run-1" and run["channel"] == "liepin")
        assert run1_liepin["queries"][0]["query"] == "机械 设计"
        assert run1_liepin["detail"]["complete_rate"] == 0.5


if __name__ == "__main__":
    unittest.main()
