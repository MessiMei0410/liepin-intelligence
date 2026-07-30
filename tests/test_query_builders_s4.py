"""S4-3c-2（N1）渠道查询方言层契约测试。

契约来源：docs/ASA_寻访链路完整优化方案_2026-07-23.md N1 节 +
docs/ASA_KIMI_TASK_S4-3c_S4-5_2026-07-23.md S4-3c-2 节。

- X-SaaS 方言：公司词独立查询（不与任何词组合）；职能/技术词锚定对 ≤2 词；
  单查询不得含 ≥2 个公司名；总量 ≤8 组；逐条执行合并去重是 runner 既有语义。
- 猎聘方言：组合查询维持（公司 + 职能/技术词可组合），≤2 词/≤6 组，公司词不两两成对。
- 种子契约：输入真实知识库 seed_silan_tme_v1.json 的关键词组（运行时只读，
  目录可用 ASA_KNOWLEDGE_BASE_DIR 覆盖），断言双渠道输出符合各自方言。
- 回放契约：#154 第 5/6/7 轮真实查询形态（嵌套拼接除外——runner 条件重置已修）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from a_system_agent import AgentService, FakeLLM  # noqa: E402
from a_system_agent import capability_runtime, query_builders, strategy_v2  # noqa: E402
from a_system_agent.knowledge_base import normalize_client_name  # noqa: E402
from a_system_agent.query_builders import (  # noqa: E402
    LIEPIN_QUERY_MAX_COUNT,
    XSAAS_QUERY_MAX_COUNT,
    build_liepin_queries,
    build_xsaas_queries,
    is_company_token,
)


class CompanyVocabularyCoverageTest(unittest.TestCase):
    def test_company_vocabulary_covers_seed_pool_without_strategy_nesting(self) -> None:
        # round8 实证：execute_external 的 strategy 嵌套不含 step2 时，仅靠图谱词表漏种子公司
        vocab = query_builders.company_vocabulary({})
        for expected in ("mps", "矽力杰", "杰华特"):
            self.assertIn(expected, vocab)

    def test_round8_pair_queries_split_solo_with_seed_vocab(self) -> None:
        vocab = query_builders.company_vocabulary({})
        out = build_xsaas_queries(["MPS 矽力杰", "MPS 杰华特"], company_terms=vocab)
        self.assertNotIn("MPS 矽力杰", out)
        self.assertEqual(out, ["MPS", "矽力杰", "杰华特"])


class QueryPlanV1ContractTest(unittest.TestCase):
    def test_strategy_v2_carries_canonical_location_and_scenario_track_into_plan(self) -> None:
        classification = {
            "input_level": "L2",
            "anchors": {
                "scenario_track": {
                    "present": True,
                    "values": ["晶圆传输", "运动控制"],
                    "source": "jd",
                    "inferred": False,
                    "confidence": "",
                },
            },
            "missing_anchors": [],
            "trace": [],
        }
        strategy = strategy_v2.build_strategy_v2(
            {
                "strategy_summary": "运动控制软件岗位",
                "target_companies": ["示例科技"],
                "channels": {
                    "liepin": [{"query": "运动控制"}],
                    "xsaas": [{"query": "运动控制"}],
                },
            },
            classification,
            canonical_position={"location": "杭州/上海"},
        )

        plan = query_builders.compile_query_plan_v1(strategy)

        self.assertEqual(plan["dimensions"]["locations"], ["杭州", "上海"])
        self.assertEqual(plan["dimensions"]["scenarios"], ["晶圆传输", "运动控制"])
        self.assertEqual(plan["execution_semantics"]["retrieval_axes"], ["channel", "query"])
        self.assertEqual(plan["execution_semantics"]["platform_filters"], [])
        self.assertTrue(all(cell["execution_filters"] == {} for cell in plan["cells"]))

    def test_compiles_complete_strategy_grid_with_stable_identity(self) -> None:
        strategy = {
            "schema_version": "strategy_v2",
            "step2_target_pool": [
                {
                    "path": "same_layer",
                    "tier": "T1",
                    "companies": [
                        {"name": "MPS", "source": "kb_profile", "confidence": "high"},
                        {"name": "矽力杰", "source": "kb_profile", "confidence": "high"},
                    ],
                },
                {
                    "path": "adjacent",
                    "tier": "T2",
                    "companies": [
                        {"name": "联宝", "source": "kb_graph", "confidence": "medium"},
                    ],
                },
            ],
            "step3_level_mapping": {"accepted_levels": ["经理", "资深工程师"]},
            "step4_keyword_groups": [
                {"group": "power", "targets": "电源方向", "terms": ["多相控制器", "DrMOS"]},
                {"group": "function", "targets": "职能方向", "terms": ["技术市场", "产品定义"]},
            ],
            "anchors": {
                "location": {"values": ["上海", "杭州"]},
                "scenario": {"values": ["PC 电源"]},
            },
        }

        first = query_builders.compile_query_plan_v1(strategy)
        second = query_builders.compile_query_plan_v1(strategy)

        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], "query_plan_v1")
        self.assertEqual(first["plan_hash"], query_builders.query_plan_hash(first))
        self.assertEqual(first["cell_count"], len(first["cells"]))
        self.assertTrue(first["cells"])
        self.assertEqual(len({cell["cell_id"] for cell in first["cells"]}), len(first["cells"]))
        self.assertEqual({cell["channel"] for cell in first["cells"]}, {"liepin", "xsaas"})
        self.assertTrue(all(cell["locations"] == ["上海", "杭州"] for cell in first["cells"]))
        self.assertTrue(all(cell["levels"] == ["经理", "资深工程师"] for cell in first["cells"]))
        self.assertTrue(all(cell["scenarios"] == ["PC 电源"] for cell in first["cells"]))

        company_refs = {
            ref["company"]
            for cell in first["cells"]
            for ref in cell["provenance"]
            if ref["kind"] in {"company_pool", "company_keyword"}
        }
        group_refs = {
            ref["group"]
            for cell in first["cells"]
            for ref in cell["provenance"]
            if ref["kind"] in {"keyword_group", "company_keyword"}
        }
        self.assertEqual(company_refs, {"MPS", "矽力杰", "联宝"})
        self.assertEqual(group_refs, {"power", "function"})

    def test_marginal_yield_scheduler_balances_conversion_overlap_and_exploration(self) -> None:
        cells = [
            {"cell_id": "q1", "channel": "liepin", "query": "高重叠", "priority": 10, "provenance": [{"kind": "keyword_group", "group": "a"}]},
            {"cell_id": "q2", "channel": "liepin", "query": "高增量", "priority": 20, "provenance": [{"kind": "keyword_group", "group": "b"}]},
            {"cell_id": "q3", "channel": "liepin", "query": "待探索", "priority": 30, "provenance": [{"kind": "keyword_group", "group": "c"}]},
        ]
        base = {
            "schema_version": "query_plan_v1",
            "source_strategy_version": "strategy_v2",
            "dimensions": {"locations": [], "levels": [], "scenarios": []},
            "cell_count": 3,
            "cells": cells,
        }
        plan = {**base, "plan_hash": query_builders.query_plan_hash(base)}
        metrics = [
            {"channel": "liepin", "query": "高重叠", "runs": 4, "unique_yield_per_run": 10, "overlap_rate": 0.9, "business_score": 0},
            {"channel": "liepin", "query": "高增量", "runs": 2, "unique_yield_per_run": 15, "overlap_rate": 0.25, "business_score": 1.5},
        ]

        scheduled = query_builders.schedule_query_plan_v1(plan, metrics)

        self.assertEqual([cell["query"] for cell in scheduled["cells"]], ["高增量", "待探索", "高重叠"])
        self.assertEqual([cell["priority"] for cell in scheduled["cells"]], [1, 2, 3])
        self.assertEqual(scheduled["cells"][0]["base_priority"], 20)
        self.assertEqual(scheduled["cells"][1]["scheduling"]["mode"], "exploration")
        self.assertEqual(scheduled["scheduler"]["policy"], "marginal_unique_yield_v1")
        self.assertEqual(scheduled["plan_hash"], query_builders.query_plan_hash(scheduled))
        self.assertEqual(plan["cells"][0]["priority"], 10, "调度不得原地修改已生成计划")

    def test_channel_execution_entries_carry_cursor_without_changing_plan_hash(self) -> None:
        cell = {
            "cell_id": "qpc_resume",
            "channel": "liepin",
            "query": "精密 机械",
            "priority": 1,
            "provenance": [{"kind": "keyword_group", "group": "mechanical"}],
            "execution_cursor": {"page": 51},
        }
        plan = {"cells": [cell], "plan_hash": "approved-hash"}

        entries = query_builders.query_plan_channel_entries(plan, "liepin")

        self.assertEqual(entries, [{
            "cell_id": "qpc_resume", "query": "精密 机械", "cursor": {"page": 51},
            "collected_before": 0,
            "evaluation_constraints": {"locations": [], "levels": [], "scenarios": []},
            "execution_filters": {},
            "seen_candidate_keys": [],
        }])
        self.assertEqual(plan["plan_hash"], "approved-hash")


def _company_count(query: str, vocab: set[str]) -> int:
    return sum(1 for token in query.split() if is_company_token(token, vocab))


def _assert_xsaas_dialect(testcase: unittest.TestCase, queries: list[str], vocab: set[str]) -> None:
    """X-SaaS 方言不变量：无 ≥2 公司名；公司词均独立查询；非公司词查询 ≤2 词。"""
    for query in queries:
        terms = query.split()
        with testcase.subTest(query=query):
            testcase.assertLessEqual(_company_count(query, vocab), 1, "单查询不得含 ≥2 个公司名")
            if _company_count(query, vocab) == 1:
                testcase.assertEqual(len(terms), 1, "公司词必须是独立查询，不与任何词组合")
            else:
                testcase.assertLessEqual(len(terms), 2, "非公司词查询 ≤2 词（锚定对）")
    testcase.assertLessEqual(len(queries), XSAAS_QUERY_MAX_COUNT)


def _assert_liepin_dialect(testcase: unittest.TestCase, queries: list[str], vocab: set[str]) -> None:
    """猎聘方言不变量：无 ≥2 公司名同组；每组 ≤2 词。"""
    for query in queries:
        with testcase.subTest(query=query):
            testcase.assertLessEqual(_company_count(query, vocab), 1, "公司词不两两成对")
            testcase.assertLessEqual(len(query.split()), 2)
    testcase.assertLessEqual(len(queries), LIEPIN_QUERY_MAX_COUNT)


class XsaasDialectTest(unittest.TestCase):
    """X-SaaS builder 单元规则（公司词表来自策略目标池/图谱，这里直接给定）。"""

    VOCAB = {"mps", "矽力杰", "杰华特"}

    def test_companies_always_solo(self) -> None:
        out = build_xsaas_queries(["MPS 矽力杰 杰华特 FAE AE"], company_terms=self.VOCAB)
        self.assertEqual(out, ["MPS", "矽力杰", "杰华特"])

    def test_company_never_combined_with_function_term(self) -> None:
        out = build_xsaas_queries(["杰华特 技术市场"], company_terms=self.VOCAB)
        self.assertEqual(out, ["杰华特", "技术市场"])

    def test_dense_noncompany_query_uses_distinctive_atomic_terms(self) -> None:
        out = build_xsaas_queries(["多相控制器 DrMOS POL TME FAE"])
        self.assertEqual(out, ["多相控制器", "DrMOS"])

    def test_pc_power_queries_prioritize_companies_and_skip_ambiguous_acronyms(self) -> None:
        out = build_xsaas_queries(
            [
                "PC 电源 TME",
                "多相控制器 DrMOS POL",
                "联想 联宝 电源工程师",
                "MPS 矽力杰 杰华特 FAE/AE/TME",
            ],
            company_terms={*self.VOCAB, "联想", "联宝"},
        )
        self.assertEqual(out, ["联想", "联宝", "MPS", "矽力杰", "杰华特", "PC电源", "多相控制器", "DrMOS"])

    def test_dict_entries_use_query_field_and_malformed_safe(self) -> None:
        items = [
            {"evidence": "岗位要求", "purpose": "精准", "query": "MPS 矽力杰", "round": "core"},
            {"evidence": "缺 query 字段"},
            "",
            None,
            "  ",
        ]
        self.assertEqual(build_xsaas_queries(items, company_terms=self.VOCAB), ["MPS", "矽力杰"])
        self.assertEqual(build_xsaas_queries([]), [])
        for query in build_xsaas_queries(items, company_terms=self.VOCAB):
            self.assertFalse(query.startswith("{'"), "字典项不得产生 repr 残片查询")

    def test_count_cap_keeps_companies_first(self) -> None:
        # 公司词优先于锚定对：超帽先砍职能/技术对，公司定向查询不丢
        out = build_xsaas_queries(["MPS 矽力杰 杰华特 多相控制器 DrMOS POL eFuse 板级电源 三次电源"], company_terms=self.VOCAB)
        self.assertEqual(out[:3], ["MPS", "矽力杰", "杰华特"])
        self.assertEqual(len(out), XSAAS_QUERY_MAX_COUNT)
        _assert_xsaas_dialect(self, out, self.VOCAB)

    def test_no_two_companies_invariant_without_vocab_degrades_safely(self) -> None:
        # 词表为空时无法识别公司词（降级）：仍按原子词输出且不抛异常
        out = build_xsaas_queries(["MPS 矽力杰 FAE"])
        self.assertEqual(out, ["MPS", "矽力杰"])
        for query in out:
            self.assertLessEqual(len(query.split()), 2)


class LiepinDialectTest(unittest.TestCase):
    """猎聘 builder 单元规则：组合查询维持，公司词不两两成对。"""

    VOCAB = {"mps", "矽力杰", "杰华特"}

    def test_company_combines_with_first_function_term(self) -> None:
        out = build_liepin_queries(["杰华特 技术市场 产品定义"], company_terms=self.VOCAB)
        self.assertEqual(out, ["杰华特 技术市场", "技术市场 产品定义"])

    def test_companies_never_paired(self) -> None:
        out = build_liepin_queries(["MPS 矽力杰 杰华特 FAE AE"], company_terms=self.VOCAB)
        self.assertEqual(out, ["MPS FAE", "矽力杰 FAE", "杰华特 FAE", "FAE AE"])
        _assert_liepin_dialect(self, out, self.VOCAB)

    def test_pure_company_group_goes_solo(self) -> None:
        self.assertEqual(build_liepin_queries(["MPS 矽力杰"], company_terms=self.VOCAB), ["MPS", "矽力杰"])

    def test_count_cap_six(self) -> None:
        out = build_liepin_queries(["多相控制器 DrMOS POL TME FAE eFuse 板级电源 三次电源"])
        self.assertEqual(len(out), LIEPIN_QUERY_MAX_COUNT)
        self.assertEqual(out[0], "多相控制器 DrMOS")


class SeedSilanDialectContractTest(unittest.TestCase):
    """种子契约：真实 seed_silan_tme_v1.json 关键词组 → 双渠道输出符合各自方言。

    知识库运行时只读（默认 /Users/messi/Documents/ASA/knowledge_base，
    可用 ASA_KNOWLEDGE_BASE_DIR 覆盖）；种子缺失则跳过。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.seed_path = strategy_v2.knowledge_base_dir() / "seed_silan_tme_v1.json"
        if not cls.seed_path.is_file():
            raise unittest.SkipTest(f"知识库种子缺失：{cls.seed_path}")
        archetypes, trace = strategy_v2.load_job_archetypes()
        cls.archetype = next((a for a in archetypes if a.get("source_file") == "seed_silan_tme_v1.json"), None)
        if cls.archetype is None:
            raise unittest.SkipTest(f"种子未被 load_job_archetypes 加载：{trace}")
        cls.vocab = cls._seed_vocab(cls.archetype)
        cls.dense_groups = [
            " ".join(group["terms"])
            for group in cls.archetype["keyword_groups"]
            if isinstance(group, dict) and group.get("terms")
        ]
        if not cls.dense_groups:
            raise unittest.SkipTest("种子无 keyword_groups")

    @staticmethod
    def _seed_vocab(archetype: dict) -> set[str]:
        """公司词表：种子 T1/T2/T3 目标池公司名（normalize_client_name 口径）。"""
        vocab: set[str] = set()
        pool = archetype.get("target_company_pool") or {}
        for tier in pool.values():
            companies = tier.get("companies") if isinstance(tier, dict) else []
            for comp in companies or []:
                name = comp.get("name") if isinstance(comp, dict) else comp
                norm = normalize_client_name(name)
                if norm:
                    vocab.add(norm)
        return vocab

    def test_vocab_covers_t1_companies(self) -> None:
        # 词表构建自检：T1 四家公司必须可判定（MPS（芯源系统）→ mps）
        for expected in ("mps", "矽力杰", "杰华特", "晶丰明源"):
            self.assertIn(expected, self.vocab)
        self.assertTrue(is_company_token("MPS", self.vocab))
        self.assertFalse(is_company_token("TME", self.vocab))
        self.assertFalse(is_company_token("多相控制器", self.vocab))

    def test_xsaas_output_conforms_per_group(self) -> None:
        for dense in self.dense_groups:
            with self.subTest(group=dense):
                _assert_xsaas_dialect(self, build_xsaas_queries([dense], company_terms=self.vocab), self.vocab)

    def test_xsaas_output_conforms_combined_groups(self) -> None:
        out = build_xsaas_queries(self.dense_groups, company_terms=self.vocab)
        _assert_xsaas_dialect(self, out, self.vocab)
        self.assertTrue(out, "种子关键词组必须产出非空 X-SaaS 查询")

    def test_xsaas_t1_companies_are_standalone_queries(self) -> None:
        # competitor_tme 组：T1 公司词全部独立成条且优先于锚定对
        competitor = next(dense for dense in self.dense_groups if "矽力杰" in dense and "TME" in dense)
        out = build_xsaas_queries([competitor], company_terms=self.vocab)
        self.assertEqual(out[:4], ["MPS", "矽力杰", "杰华特", "晶丰明源"])

    def test_liepin_output_conforms_per_group_and_combined(self) -> None:
        for dense in self.dense_groups:
            with self.subTest(group=dense):
                _assert_liepin_dialect(self, build_liepin_queries([dense], company_terms=self.vocab), self.vocab)
        out = build_liepin_queries(self.dense_groups, company_terms=self.vocab)
        _assert_liepin_dialect(self, out, self.vocab)
        self.assertTrue(out, "种子关键词组必须产出非空猎聘查询")

    def test_dialects_diverge_on_company_groups(self) -> None:
        # 方言差异契约：含公司词的组，猎聘保留"公司+职能词"组合，X-SaaS 拆成公司独立查询
        competitor = next(dense for dense in self.dense_groups if "矽力杰" in dense and "TME" in dense)
        liepin = build_liepin_queries([competitor], company_terms=self.vocab)
        xsaas = build_xsaas_queries([competitor], company_terms=self.vocab)
        self.assertNotEqual(liepin, xsaas)
        self.assertTrue(any(query.startswith("MPS ") for query in liepin), "猎聘保留公司+职能词组合")
        self.assertTrue(all(not query.startswith("MPS ") for query in xsaas), "X-SaaS 公司词不组合")


class RoundReplayDialectTest(unittest.TestCase):
    """#154 第 5/6/7 轮真实查询形态回放（嵌套拼接除外——runner 条件重置已另行修复）。"""

    VOCAB = {"mps", "矽力杰", "杰华特"}

    def test_round5_dense_five_word_query(self) -> None:
        # round5 第 1 组"多相控制器 DrMOS POL TME FAE"五词直查 X-SaaS 0 条实证
        dense = "多相控制器 DrMOS POL TME FAE"
        xsaas = build_xsaas_queries([dense], company_terms=self.VOCAB)
        self.assertEqual(xsaas, ["多相控制器", "DrMOS"])
        liepin = build_liepin_queries([dense], company_terms=self.VOCAB)
        self.assertNotEqual(liepin, xsaas)
        for query in xsaas:
            self.assertLessEqual(len(query.split()), 2)

    def test_round6_dict_entries_take_query_field(self) -> None:
        # round6 真实形态：LLM 策略步骤产出 {evidence, purpose, query, round} 字典项
        items = [
            {"evidence": "岗位要求熟悉多相控制器", "purpose": "精准锁定", "query": "多相控制器 DrMOS POL TME", "round": "core"},
            {"query": "MPS 矽力杰"},
            {"evidence": "缺 query 字段"},
        ]
        xsaas = build_xsaas_queries(items, company_terms=self.VOCAB)
        self.assertEqual(
            xsaas,
            ["MPS", "矽力杰", "多相控制器", "DrMOS"],
        )
        _assert_xsaas_dialect(self, xsaas, self.VOCAB)

    def test_round7_pure_company_group_and_mixed_group(self) -> None:
        # round7 真实形态：纯公司组与公司+职能混合组（单查询 ≥2 公司名语义必错）
        pure = build_xsaas_queries(["MPS 矽力杰"], company_terms=self.VOCAB)
        self.assertEqual(pure, ["MPS", "矽力杰"])
        mixed_xsaas = build_xsaas_queries(["MPS 矽力杰 杰华特 FAE AE"], company_terms=self.VOCAB)
        self.assertEqual(mixed_xsaas, ["MPS", "矽力杰", "杰华特"])
        mixed_liepin = build_liepin_queries(["MPS 矽力杰 杰华特 FAE AE"], company_terms=self.VOCAB)
        self.assertEqual(mixed_liepin, ["MPS FAE", "矽力杰 FAE", "杰华特 FAE", "FAE AE"])
        _assert_xsaas_dialect(self, pure + mixed_xsaas, self.VOCAB)
        _assert_liepin_dialect(self, mixed_liepin, self.VOCAB)


class CapabilityRuntimeThinWiringTest(unittest.TestCase):
    """薄接线契约：capability_runtime 只委托，方言规则的唯一来源是 query_builders。"""

    def test_constants_are_aliases(self) -> None:
        self.assertIs(capability_runtime.LIEPIN_QUERY_MAX_TERMS, query_builders.LIEPIN_QUERY_MAX_TERMS)
        self.assertIs(capability_runtime.LIEPIN_QUERY_MAX_COUNT, query_builders.LIEPIN_QUERY_MAX_COUNT)
        self.assertIs(capability_runtime.XSAAS_QUERY_MAX_TERMS, query_builders.XSAAS_QUERY_MAX_TERMS)
        self.assertIs(capability_runtime.XSAAS_QUERY_MAX_COUNT, query_builders.XSAAS_QUERY_MAX_COUNT)

    def test_adapt_channel_queries_delegates(self) -> None:
        sample = ["MPS 矽力杰 FAE AE", {"query": "多相控制器 DrMOS POL"}, "", None]
        for kwargs in (
            {"max_terms": 2, "max_count": 6},
            {"max_terms": 2, "max_count": 8, "company_terms": {"mps", "矽力杰"}},
        ):
            self.assertEqual(
                capability_runtime.adapt_channel_queries(sample, **kwargs),
                query_builders.adapt_queries(sample, **kwargs),
            )


class ExecuteExternalDialectWiringTest(unittest.TestCase):
    """端到端接线契约：execute_external 写入 runner 的查询文件符合各渠道方言。"""

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
        conn.execute("INSERT INTO clients VALUES (1,'士兰微')")
        conn.execute("INSERT INTO jobs VALUES (10,1,'技术市场经理')")
        conn.commit()
        conn.close()
        self.service = AgentService(self.db_path, FakeLLM({}))
        # 隔离真实图谱（company_vocabulary 会读知识库）：空目录降级为空图谱，断言确定
        self._old_kb_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = self.temp.name

    def tearDown(self) -> None:
        if self._old_kb_env is None:
            os.environ.pop("ASA_KNOWLEDGE_BASE_DIR", None)
        else:
            os.environ["ASA_KNOWLEDGE_BASE_DIR"] = self._old_kb_env
        self.service.close()
        self.temp.cleanup()

    def _install_fake_runners(self) -> None:
        runtime = self.service.capability_runtime

        def fake_run_json(command: list[str], timeout: int = 300) -> dict:
            cmd = [str(part) for part in command]
            if "--json-output" in cmd:
                out = Path(cmd[cmd.index("--json-output") + 1])
                out.write_text("[]", encoding="utf-8")
                return {"ok": True, "candidates": 0, "rounds": [{"query": "q", "result_count": 0, "extracted_count": 0}]}
            if "--output" in cmd:
                out = Path(cmd[cmd.index("--output") + 1])
                out.write_text("[]", encoding="utf-8")
                return {"ok": True, "candidates": 0, "rounds": [{"query": "q", "status": "completed", "result_count": 0, "extracted_count": 0}]}
            if "intake" in cmd:
                if "--apply" in cmd:
                    return {"staged": {"accepted": []}, "intake": {"inserted": 0, "receipts": []}}
                return {"staged": {"accepted_count": 0}, "intake": {"applied": False, "inserted": 0}}
            raise AssertionError(f"unexpected command: {cmd}")

        runtime._run_json = fake_run_json  # type: ignore[method-assign]
        runtime._run = lambda command, timeout=300: subprocess.CompletedProcess(command, 0, stdout="sync ok", stderr="")  # type: ignore[method-assign]

    @staticmethod
    def _approved_plan(liepin: list[str], xsaas: list[str]) -> dict:
        cells = [
            {
                "cell_id": f"qpc_test_{channel}_{index}",
                "channel": channel,
                "query": query,
                "locations": [],
                "levels": [],
                "scenarios": [],
                "priority": index,
                "provenance": [{"kind": "keyword_group", "group": "test"}],
            }
            for channel, queries in (("liepin", liepin), ("xsaas", xsaas))
            for index, query in enumerate(queries, 1)
        ]
        plan = {
            "schema_version": "query_plan_v1",
            "source_strategy_version": "strategy_v2",
            "dimensions": {"locations": [], "levels": [], "scenarios": []},
            "cell_count": len(cells),
            "cells": cells,
        }
        return {**plan, "plan_hash": query_builders.query_plan_hash(plan)}

    def test_channel_query_files_follow_dialects(self) -> None:
        self._install_fake_runners()
        dense = "多相控制器 DrMOS POL TME FAE"
        mixed = "MPS 矽力杰 FAE"
        expected_liepin = [
            "MPS FAE", "矽力杰 FAE", "多相控制器 DrMOS",
            "多相控制器 POL", "多相控制器 TME", "多相控制器 FAE",
        ]
        expected_xsaas = ["MPS", "矽力杰", "多相控制器", "DrMOS"]
        approved_plan = self._approved_plan(expected_liepin, expected_xsaas)
        request = {
            "client": "士兰微",
            "job": "技术市场经理",
            "target_count": 5,
            "max_query_cells_per_batch": 20,
            "workflow_id": "wf-dialect",
            "opencli_shadow": False,
            "query_plan_v1": approved_plan,
            "query_plan_hash": approved_plan["plan_hash"],
            "strategy": {
                "channels": {
                    "liepin": ["审批后不得执行的旧查询", mixed, {"query": dense}],
                    "xsaas": ["审批后不得执行的旧查询", mixed, {"query": dense}],
                },
                "strategy_v2": {
                    "step2_target_pool": [{"companies": [{"name": "MPS"}, {"name": "矽力杰"}]}],
                },
            },
        }
        self.service.capability_runtime.execute_external("multi_channel_sourcing", request)

        sourcing_dir = self.service.capability_runtime.output_dir / "sourcing"
        liepin_file = next(sourcing_dir.glob("*-liepin-queries.json"))
        xsaas_file = next(sourcing_dir.glob("*-xsaas-queries.json"))
        liepin_entries = json.loads(liepin_file.read_text(encoding="utf-8"))["queries"]
        xsaas_entries = json.loads(xsaas_file.read_text(encoding="utf-8"))["queries"]
        liepin = [entry["query"] for entry in liepin_entries]
        xsaas = [entry["query"] for entry in xsaas_entries]
        vocab = {"mps", "矽力杰"}

        self.assertTrue(all(entry["execution_filters"] == {} for entry in [*liepin_entries, *xsaas_entries]))
        self.assertTrue(all("evaluation_constraints" in entry for entry in [*liepin_entries, *xsaas_entries]))

        # 猎聘：组合查询维持（公司+职能词成对），≤2 词/≤6 组
        self.assertEqual(
            liepin,
            expected_liepin,
        )
        _assert_liepin_dialect(self, liepin, vocab)
        # X-SaaS：公司词优先独立查询，过滤裸歧义缩写，技术词使用原子查询
        self.assertEqual(
            xsaas,
            expected_xsaas,
        )
        _assert_xsaas_dialect(self, xsaas, vocab)
        self.assertNotEqual(liepin, xsaas, "双渠道方言必须在接线层分叉")

    def test_missing_approved_query_plan_is_blocked_without_fallback(self) -> None:
        self._install_fake_runners()
        request = {
            "client": "士兰微",
            "job": "技术市场经理",
            "target_count": 5,
            "workflow_id": "wf-no-approved-plan",
            "opencli_shadow": False,
            "strategy": {
                "channels": {
                    "liepin": ["不得执行的旧查询"],
                    "xsaas": ["不得执行的旧查询"],
                },
            },
        }

        with self.assertRaisesRegex(ValueError, "批准的 query_plan_v1"):
            self.service.capability_runtime.execute_external("multi_channel_sourcing", request)


if __name__ == "__main__":
    unittest.main()
