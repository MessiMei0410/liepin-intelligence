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

from a_system_agent import AgentService, FakeLLM  # noqa: E402
from a_system_agent.capability_runtime import (  # noqa: E402
    ZERO_RESULT_ATTRIBUTIONS,
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
            "loading_incomplete": [
                ("xsaas", "blocked", {"ok": False, "error": "X-SaaS 候选人列表加载超时"}),
                ("xsaas", "completed", {"ok": True, "rounds": [{"query": "q", "status": "stale_query", "result_count": 8, "extracted_count": 0}]}),
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
        xsaas_result: dict | Exception,
        xsaas_candidates: list[dict] | None = None,
        intake_apply: dict | None = None,
    ) -> None:
        runtime = self.service.capability_runtime

        def fake_run_json(command: list[str], timeout: int = 300) -> dict:
            cmd = [str(part) for part in command]
            if "--json-output" in cmd:
                if isinstance(liepin_result, Exception):
                    raise liepin_result
                out = Path(cmd[cmd.index("--json-output") + 1])
                out.write_text(json.dumps(liepin_candidates or [], ensure_ascii=False), encoding="utf-8")
                return liepin_result
            if "--output" in cmd:
                if isinstance(xsaas_result, Exception):
                    raise xsaas_result
                out = Path(cmd[cmd.index("--output") + 1])
                out.write_text(json.dumps(xsaas_candidates or [], ensure_ascii=False), encoding="utf-8")
                return xsaas_result
            if "intake" in cmd:
                if "--apply" in cmd:
                    return intake_apply or {"staged": {"accepted": []}, "intake": {"inserted": 0, "receipts": []}}
                return {"staged": {"accepted_count": 0}, "intake": {"applied": False, "inserted": 0}}
            raise AssertionError(f"unexpected command: {cmd}")

        runtime._run_json = fake_run_json  # type: ignore[method-assign]
        runtime._run = lambda command, timeout=300: subprocess.CompletedProcess(command, 0, stdout="sync ok", stderr="")  # type: ignore[method-assign]

    def _request(self) -> dict:
        return {
            "client": "长越科技",
            "job": "机械高级工程师",
            "target_count": 5,
            "workflow_id": "wf-test",
            "opencli_shadow": False,
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
            xsaas_result={
                "ok": True,
                "candidates": 1,
                "rounds": [{"query": "机械工程师", "status": "completed", "result_count": 5, "extracted_count": 3}],
                "detail_capture": {"requested": 1, "complete": 0, "partial": 1, "failed": 0},
            },
            xsaas_candidates=[{"channel": "xsaas", "name": "王五", "query": "机械工程师"}],
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
        assert liepin["status"] == "completed"
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

    def test_liepin_failure_records_failed_funnel_row_and_reraises(self) -> None:
        self._install_runners(
            liepin_result=RuntimeError("猎聘登录已过期，请在 Chrome 里登录猎聘后再继续。"),
            xsaas_result={"ok": True, "candidates": 0, "rounds": []},
        )
        with self.assertRaises(RuntimeError):
            self.service.capability_runtime.execute_external("multi_channel_sourcing", self._request())
        rows = self._funnel_rows()
        assert len(rows) == 1
        assert rows[0]["channel"] == "liepin"
        assert rows[0]["status"] == "failed"
        assert rows[0]["zero_attribution"] == "session_expired"
        assert "登录已过期" in rows[0]["error"]

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
