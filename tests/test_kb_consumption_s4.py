"""S4-2：知识库消费 —— 客户画像挂载 + 公司图谱查询 + restricted 层边界测试。

口径：docs/ASA_PRD_S4_sourcing_strategy_agent_2026-07-23.md §3/§8。
全部使用临时库 + 临时 KB fixture（运行时只读，绝不触碰真实知识库与生产 DB）。
restricted 边界为 P0：费率/手机号/offer 金额/话术红线字面量不得出现在任何对外路径。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from test_a_system_agent_v1 import AgentDbCase, fake_assessment
from a_system_agent import AgentService, FakeLLM
from a_system_agent import knowledge_base, strategy_v2
from asa_core.service import CoreService


KB_PROFILES_FIXTURE = {
    "meta": {"version": "test", "usage": "S4-2 测试 fixture"},
    "profiles": [
        {
            "client": "长川科技",
            "track": "质量控制设备",
            "sub_track": "后道装备",
            "process_track": "测试设备, 分选机设备, 后道检测",
            "selling_points": "大陆第一家集成电路封测装备上市公司，国内封测设备龙头。",
            "interview_process": "2-3轮（HR初面-部门技术面-总监综面），可线上面试。",
            "interviewer_style": "注重人选稳定性，杭州本地优先，能接受加班。",
            "competitor_target": "泰瑞达、爱德万、华峰测控、新益昌等测试设备厂商。",
            "rec_notes": "大小周工作制，研发加班较多。",
            "rate": "20%",
            "website": "https://example.com/changchuan",
            "schedule": "早八点半晚五点半，大小周。",
            "comp_benefits": "工程师级15薪。",
        },
        {"client": "士兰微", "track": "功率半导体", "selling_points": "IDM 龙头"},
    ],
}

KB_GRAPH_FIXTURE = {
    "meta": {"version": "test"},
    "companies": {
        "杭州鲁滨逊测试技术有限公司": {
            "track": "测试量测设备｜测试/分选/AOI/量测",
            "business": "半导体测试机、分选机与后道检测设备研发制造",
            "categories": ["半导体设备"],
        },
        "上海福尔摩斯量测仪器有限公司": {
            "track": "测试量测设备｜测试/分选/AOI/量测",
            "business": "AOI 光学量测仪器",
            "categories": ["精密仪器"],
        },
        "苏州刻蚀先锋科技有限公司": {
            "track": "前道设备｜刻蚀/等离子",
            "business": "等离子刻蚀装备",
            "categories": ["半导体设备"],
        },
        "杭州长川科技股份有限公司": {
            "track": "测试量测设备｜测试/分选/AOI/量测",
            "business": "测试机、分选机、探针台研发制造",
            "categories": ["半导体设备"],
        },
    },
}

KB_CASE_FIXTURE = {
    "meta": {"case_id": "case_pengxinxu_fab", "governance": "restricted 层测试 fixture"},
    "client_profile": {"name": "深圳市鹏新旭技术有限公司"},
    "restricted": {
        "banned_companies": ["青岛芯恩", "福建晋华", "楚芯"],
        "banned_rule": "指目前在职；已离职可正常推荐",
        "scripts_redline": "话术红线MARKER_REDLINE_X7：意向不明确之前不用讲做什么",
        "consultant_phone": "13912345678",
        "fee_rate": "费率23%",
        "offer_amounts": ["offer金额 年薪120万", "offer金额 年薪95万"],
        "non_compete_companies": ["某竞业对手甲"],
        "pii_note": "顾问认领表含手机号，未抽取入库",
    },
}

# restricted 白名单键（与 knowledge_base 模块口径一致）：这些键值允许进入策略约束
_RESTRICTED_ALLOWED_FOR_TEST = {"banned_companies", "banned_rule", "non_compete_companies"}


def _write_kb(
    base: Path,
    *,
    profiles: dict | None = KB_PROFILES_FIXTURE,
    graph: dict | None = KB_GRAPH_FIXTURE,
    case: dict | None = KB_CASE_FIXTURE,
    broken_graph: bool = False,
    broken_profiles: bool = False,
) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    if profiles is not None:
        target = base / "kb_client_profiles_v1.json"
        target.write_text("{这不是合法JSON" if broken_profiles else json.dumps(profiles, ensure_ascii=False), encoding="utf-8")
    if graph is not None:
        target = base / "kb_company_graph_jsj_v1.json"
        target.write_text("{这不是合法JSON" if broken_graph else json.dumps(graph, ensure_ascii=False), encoding="utf-8")
    if case is not None:
        (base / "cases").mkdir(parents=True, exist_ok=True)
        (base / "cases" / "case_pengxinxu_fab_v1.json").write_text(
            json.dumps(case, ensure_ascii=False), encoding="utf-8"
        )
    return base


def _forbidden_literals(case_doc: dict = KB_CASE_FIXTURE) -> list[str]:
    """遍历 case fixture 的 restricted 字段值：白名单键之外的全部字面量 + 原子敏感片段
    （手机号/费率/金额/红线标记）。契约测试断言这些绝不出现在对外路径。"""
    restricted = case_doc["restricted"]
    literals: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, str):
            if value.strip():
                literals.append(value)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)

    for key, value in restricted.items():
        if key in _RESTRICTED_ALLOWED_FOR_TEST:
            continue
        walk(value)
    atoms: set[str] = set()
    for literal in literals:
        atoms.update(re.findall(r"1[3-9]\d{9}", literal))
        atoms.update(re.findall(r"\d+(?:\.\d+)?%", literal))
        atoms.update(re.findall(r"\d+(?:\.\d+)?万", literal))
        atoms.update(re.findall(r"MARKER[A-Z0-9_]+", literal))
    return list(dict.fromkeys([*literals, *sorted(atoms)]))


class ClientProfileMatchTest(unittest.TestCase):
    """画像三级匹配：精确 → 去括号/规范化别名 → 模糊需人工确认；不匹配与降级留痕。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.kb_dir = _write_kb(Path(self.temp.name) / "kb")
        self.profiles, self.load_trace = knowledge_base.load_client_profiles(self.kb_dir)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_exact_match(self) -> None:
        match, trace = knowledge_base.match_client_profile("长川科技", self.profiles)
        assert match is not None
        assert match["rule"] == "exact"
        assert match["needs_confirmation"] is False
        assert match["name"] == "长川科技"
        assert any("命中画像" in line and "exact" in line for line in trace)

    def test_alias_match_normalized(self) -> None:
        match, _ = knowledge_base.match_client_profile("杭州长川科技股份有限公司", self.profiles)
        assert match is not None
        assert match["rule"] == "alias"
        assert match["needs_confirmation"] is False
        assert match["name"] == "长川科技"
        # 带括号别名同样命中
        match2, _ = knowledge_base.match_client_profile("长川科技（杭州）", self.profiles)
        assert match2 is not None and match2["rule"] == "alias"

    def test_fuzzy_match_requires_confirmation(self) -> None:
        match, trace = knowledge_base.match_client_profile("长川", self.profiles)
        assert match is not None
        assert match["rule"] == "fuzzy"
        assert match["needs_confirmation"] is True, "模糊匹配必须标记需人工确认，不得静默命中"
        assert match["name"] == "长川科技"
        assert any("需人工确认" in line for line in trace)

    def test_no_match(self) -> None:
        match, trace = knowledge_base.match_client_profile("某某快消公司", self.profiles)
        assert match is None
        assert any("未命中" in line for line in trace)
        info = knowledge_base.profile_matched_info(match)
        assert info == {"name": "", "rule": "none", "needs_confirmation": False}

    def test_missing_or_broken_profiles_degrade_with_trace(self) -> None:
        missing_dir = Path(tempfile.mkdtemp()) / "no_such_dir"
        profiles, trace = knowledge_base.load_client_profiles(missing_dir)
        assert profiles == []
        assert any("按无画像处理" in line for line in trace)

        broken_base = Path(tempfile.mkdtemp()) / "kb"
        _write_kb(broken_base, graph=None, case=None, broken_profiles=True)
        profiles, trace = knowledge_base.load_client_profiles(broken_base)
        assert profiles == []
        assert any("解析失败" in line and "按无画像处理" in line for line in trace)


class ProfileContextWhitelistTest(unittest.TestCase):
    """画像注入上下文只含白名单六类；费率/作息/薪资福利等绝不注入。"""

    def test_context_contains_only_whitelist_fields(self) -> None:
        profile = KB_PROFILES_FIXTURE["profiles"][0]
        context = knowledge_base.profile_context(profile)
        assert set(context) <= set(knowledge_base.PROFILE_CONTEXT_FIELDS)
        assert "封测设备龙头" in context["selling_points"]
        assert "测试设备" in context["track"]
        assert "HR初面" in context["interview_process"]
        assert "稳定性" in context["hiring_preferences"]
        assert "泰瑞达" in context["target_pool_hint"]
        assert "大小周" in context["notes"]
        encoded = json.dumps(context, ensure_ascii=False)
        assert "20%" not in encoded, "费率 restricted，不得注入"
        assert "早八点半" not in encoded
        assert "15薪" not in encoded
        assert "https://example.com" not in encoded


class CompanyGraphSearchTest(unittest.TestCase):
    """图谱检索：赛道/主营业务/四分类标签；confidence 分档；缺失/坏 JSON 降级留痕。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.kb_dir = _write_kb(Path(self.temp.name) / "kb")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_load_graph(self) -> None:
        graph, trace = knowledge_base.load_company_graph(self.kb_dir)
        assert len(graph) == 4
        assert graph["杭州鲁滨逊测试技术有限公司"]["categories"] == ["半导体设备"]
        assert any("已加载公司图谱 4 家" in line for line in trace)

    def test_search_by_track_and_business(self) -> None:
        graph, _ = knowledge_base.load_company_graph(self.kb_dir)
        hits = knowledge_base.search_companies(graph, query_text="测试设备 分选机设备 后道检测")
        names = [hit["name"] for hit in hits]
        assert "杭州鲁滨逊测试技术有限公司" in names
        assert names[0] == "杭州鲁滨逊测试技术有限公司", "重合度最高的公司排最前"
        top = hits[0]
        assert top["confidence"] == "high"
        assert top["matched_tokens"], "命中 token 可解释留痕"
        assert "苏州刻蚀先锋科技有限公司" not in names, "赛道不相干的公司不得召回"
        confidence_by_name = {hit["name"]: hit["confidence"] for hit in hits}
        assert confidence_by_name.get("上海福尔摩斯量测仪器有限公司") in {"high", "medium"}

    def test_search_by_categories(self) -> None:
        graph, _ = knowledge_base.load_company_graph(self.kb_dir)
        hits = knowledge_base.search_companies(graph, query_text="量测", categories=["精密仪器"])
        assert [hit["name"] for hit in hits] == ["上海福尔摩斯量测仪器有限公司"]

    def test_derive_graph_pool_source_and_confidence(self) -> None:
        graph, _ = knowledge_base.load_company_graph(self.kb_dir)
        pool, trace = knowledge_base.derive_graph_pool(graph, query_text="测试设备 分选机设备 后道检测")
        assert pool
        assert all(company["source"] == "kb_graph" for company in pool)
        assert all(company["confidence"] in {"high", "medium", "low"} for company in pool)
        assert any("召回" in line and "核验本人证据" in line for line in trace)

    def test_missing_or_broken_graph_degrades_to_empty(self) -> None:
        missing_dir = Path(tempfile.mkdtemp()) / "no_such_dir"
        graph, trace = knowledge_base.load_company_graph(missing_dir)
        assert graph == {}
        assert any("降级为空图谱" in line for line in trace)

        broken_base = Path(tempfile.mkdtemp()) / "kb"
        _write_kb(broken_base, profiles=None, case=None, broken_graph=True)
        graph, trace = knowledge_base.load_company_graph(broken_base)
        assert graph == {}
        assert any("解析失败" in line and "降级为空图谱" in line for line in trace)


class RestrictedReaderTest(unittest.TestCase):
    """restricted 白名单读取：仅禁挖名单/竞业限制出库；其余键值永远不出库。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.kb_dir = _write_kb(Path(self.temp.name) / "kb")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_whitelist_extraction(self) -> None:
        info, trace = knowledge_base.load_restricted_constraints("深圳市鹏新旭技术有限公司", kb_dir=self.kb_dir)
        assert info is not None
        assert info["matched_by"] == "exact"
        constraints = info["constraints"]
        assert constraints["banned_companies"] == ["青岛芯恩", "福建晋华", "楚芯"]
        assert "已离职可正常推荐" in constraints["banned_rule"]
        assert constraints["non_compete_companies"] == ["某竞业对手甲"]
        for key in ("scripts_redline", "consultant_phone", "fee_rate", "offer_amounts", "pii_note"):
            assert key in info["skipped_keys"], f"{key} 必须被拦截留痕"
            assert key not in constraints
        encoded = json.dumps(info, ensure_ascii=False)
        for literal in _forbidden_literals():
            assert literal not in encoded, f"restricted 受限字面量不得出库：{literal}"
        assert any("白名单" in line and "拦截" in line for line in trace)

    def test_alias_client_match_and_no_fuzzy_for_restricted(self) -> None:
        info, _ = knowledge_base.load_restricted_constraints("鹏新旭", kb_dir=self.kb_dir)
        assert info is not None and info["matched_by"] == "alias"
        # 不相干客户不得错配（restricted 宁可 miss 不可错配）
        info, trace = knowledge_base.load_restricted_constraints("长川科技", kb_dir=self.kb_dir)
        assert info is None
        assert any("无 restricted 层约束" in line for line in trace)

    def test_missing_cases_dir_degrades(self) -> None:
        empty = Path(tempfile.mkdtemp()) / "kb"
        empty.mkdir()
        info, trace = knowledge_base.load_restricted_constraints("鹏新旭", kb_dir=empty)
        assert info is None
        assert any("按无客户约束处理" in line for line in trace)

    def test_restricted_negative_rules(self) -> None:
        info, _ = knowledge_base.load_restricted_constraints("鹏新旭", kb_dir=self.kb_dir)
        rules = knowledge_base.restricted_negative_rules(info)
        assert len(rules) == 2
        assert all(rule["source"] == "restricted_client" for rule in rules)
        banned = next(rule for rule in rules if rule["type"] == "禁挖名单")
        assert "青岛芯恩" in banned["rule"] and "在职保护" in banned["rule"]
        non_compete = next(rule for rule in rules if rule["type"] == "竞业限制")
        assert "某竞业对手甲" in non_compete["rule"]
        assert knowledge_base.restricted_negative_rules(None) == []


class KbConsumptionDbCase(AgentDbCase):
    """公共 fixture：临时 KB（画像+图谱+case）+ 临时库客户/岗位。"""

    def setUp(self) -> None:
        super().setUp()
        self.kb_temp = tempfile.TemporaryDirectory()
        self.kb_dir = _write_kb(Path(self.kb_temp.name) / "kb")
        self._old_kb_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(self.kb_dir)
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO clients VALUES (5,'长川科技')")
        conn.execute(
            "INSERT INTO jobs VALUES (50,5,'机械工程师（测试机方向）','杭州','已发布','','','','','测试机设备机械设计','2026-07-23')"
        )
        conn.execute("INSERT INTO clients VALUES (6,'杭州长川科技股份有限公司')")
        conn.execute(
            "INSERT INTO jobs VALUES (60,6,'机械工程师','杭州','已发布','','','','','测试设备机械设计','2026-07-23')"
        )
        conn.execute("INSERT INTO clients VALUES (7,'长川')")
        conn.execute(
            "INSERT INTO jobs VALUES (70,7,'机械工程师','杭州','已发布','','','','','测试设备机械设计','2026-07-23')"
        )
        conn.execute("INSERT INTO clients VALUES (8,'深圳市鹏新旭技术有限公司')")
        conn.execute(
            "INSERT INTO jobs VALUES (80,8,'分析设备专家','深圳','已发布','','','','','fab 分析设备','2026-07-23')"
        )
        conn.execute("INSERT INTO clients VALUES (9,'某某公司')")
        conn.execute(
            "INSERT INTO jobs VALUES (90,9,'销售总监','上海','已发布','','','','','快消行业销售总监','2026-07-23')"
        )
        conn.commit()
        conn.close()
        self.service = AgentService(self.db_path, FakeLLM(fake_assessment(), chat_text="测试回答"))
        self.captured: dict[str, Any] = {}

    def tearDown(self) -> None:
        self.service.close()
        if self._old_kb_env is None:
            os.environ.pop("ASA_KNOWLEDGE_BASE_DIR", None)
        else:
            os.environ["ASA_KNOWLEDGE_BASE_DIR"] = self._old_kb_env
        self.kb_temp.cleanup()
        super().tearDown()

    def _run_strategy(self, job_id: int) -> dict:
        def _strategy(payload: dict) -> dict:
            self.captured["payload"] = payload
            return {
                "strategy_summary": "模型策略",
                "channels": {
                    "liepin": [{"round": "core", "query": "测试机 机械设计", "purpose": "核心", "evidence": "岗位要求"}],
                    "xsaas": [],
                },
            }

        self.service.llm = FakeLLM(fake_assessment(), search_strategy=_strategy)
        return self.service.capability_runtime.run_search_strategy(
            {"type": "job", "id": job_id}, {"objective": "补充候选人"}
        )

    def _prepare_core_tables(self) -> None:
        """为 asa_core 岗位详情补最小表结构（临时库，绝不动生产 schema）。"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS positions(id INTEGER,client TEXT,title TEXT,updated_at TEXT);
                CREATE TABLE IF NOT EXISTS job_pipeline_metrics(
                    id INTEGER,job_id INTEGER,priority TEXT,risk TEXT,stop_condition TEXT,
                    a_count INTEGER,b_count INTEGER,p0_count INTEGER,p1_count INTEGER,
                    published_count INTEGER,under_review_count INTEGER,contacted_count INTEGER,
                    pending_followup_count INTEGER,next_keywords_json TEXT,target_companies_json TEXT,
                    exclude_terms_json TEXT,data_gap TEXT);
                CREATE TABLE IF NOT EXISTS search_experiments(
                    id INTEGER,channel TEXT,query TEXT,result_count INTEGER,viewed_count INTEGER,
                    extracted_count INTEGER,recommended_count INTEGER,reply_count INTEGER,
                    positive_reply_count INTEGER,noise_notes TEXT,status TEXT,run_time TEXT,
                    updated_at TEXT,created_at TEXT,client TEXT,position TEXT);
                CREATE TABLE IF NOT EXISTS followup_tasks(
                    id INTEGER,job_candidate_id INTEGER,candidate_name TEXT,candidate_company TEXT,
                    task_type TEXT,priority TEXT,due_at TEXT,status TEXT,reason TEXT,updated_at TEXT,
                    client TEXT,position TEXT);
                """
            )
            conn.commit()
        finally:
            conn.close()


class StrategyProfileMountTest(KbConsumptionDbCase):
    """画像注入策略生成上下文（LLM 输入）+ 策略对象 profile_matched 留痕。"""

    def test_exact_mount_injects_llm_context_and_trace(self) -> None:
        result = self._run_strategy(50)
        payload = self.captured["payload"]
        mounted = payload["client_profile"]
        assert mounted["matched"] is True
        assert mounted["name"] == "长川科技"
        assert mounted["rule"] == "exact"
        assert mounted["needs_confirmation"] is False
        assert "封测设备龙头" in mounted["context"]["selling_points"]
        assert "测试设备" in mounted["context"]["track"]
        # 费率/作息/薪资福利不得进入生成上下文
        encoded = json.dumps(payload, ensure_ascii=False)
        assert "20%" not in encoded and "15薪" not in encoded and "早八点半" not in encoded

        v2 = result["strategy_v2"]
        assert v2["profile_matched"] == {"name": "长川科技", "rule": "exact", "needs_confirmation": False}
        ok, errors = strategy_v2.validate_strategy_v2(v2)
        assert ok, errors
        assert any("客户画像已挂载：长川科技" in line for line in v2["classification_trace"])
        artifact = result["artifacts"][0]
        assert artifact["metadata"]["strategy_v2"]["profile_matched"]["rule"] == "exact"

    def test_alias_mount(self) -> None:
        result = self._run_strategy(60)
        assert result["strategy_v2"]["profile_matched"] == {
            "name": "长川科技", "rule": "alias", "needs_confirmation": False,
        }

    def test_fuzzy_mount_needs_confirmation_everywhere(self) -> None:
        result = self._run_strategy(70)
        v2 = result["strategy_v2"]
        assert v2["profile_matched"]["rule"] == "fuzzy"
        assert v2["profile_matched"]["needs_confirmation"] is True
        payload = self.captured["payload"]
        assert payload["client_profile"]["needs_confirmation"] is True, "LLM 输入必须带需人工确认标记"
        assert any("需人工确认" in line for line in v2["classification_trace"])

    def test_no_match_leaves_trace(self) -> None:
        result = self._run_strategy(90)
        v2 = result["strategy_v2"]
        assert v2["profile_matched"] == {"name": "", "rule": "none", "needs_confirmation": False}
        assert self.captured["payload"]["client_profile"] == {"matched": False}
        assert any("客户画像未挂载" in line for line in v2["classification_trace"])


class StrategyGraphPoolTest(KbConsumptionDbCase):
    """图谱接入 step2：kb_graph + confidence、来源分布留痕、缺失降级。"""

    def test_graph_pool_merges_into_step2(self) -> None:
        result = self._run_strategy(50)
        v2 = result["strategy_v2"]
        graph_companies = [
            company
            for entry in v2["step2_target_pool"]
            for company in entry["companies"]
            if company["source"] == "kb_graph"
        ]
        assert graph_companies, "图谱召回公司必须进入 step2"
        assert any(company["name"] == "杭州鲁滨逊测试技术有限公司" for company in graph_companies)
        assert all(company["confidence"] in {"high", "medium", "low"} for company in graph_companies)
        assert v2["step2_source_distribution"].get("kb_graph", 0) == len(graph_companies)
        assert any("来源分布" in line and "kb_graph" in line for line in v2["classification_trace"])
        graph_entry = next(
            entry for entry in v2["step2_target_pool"] if any(c["source"] == "kb_graph" for c in entry["companies"])
        )
        assert "核验本人证据" in graph_entry["rationale"], "governance 口径必须写进池留痕"
        # 客户本公司不得进入目标池（图谱按赛道召回时本公司常高分命中）
        assert all("长川" not in company["name"] for company in graph_companies)
        assert any("剔除客户本公司" in line for line in v2["classification_trace"])
        # LLM 输入同样带图谱候选（供采用为 kb_graph 来源）
        assert any(
            company["name"] == "杭州鲁滨逊测试技术有限公司" for company in self.captured["payload"]["kb_graph_candidates"]
        )
        assert all("长川" not in company["name"] for company in self.captured["payload"]["kb_graph_candidates"])

    def test_llm_inferred_companies_stay_unconfirmed(self) -> None:
        def _strategy(payload: dict) -> dict:
            return {
                "strategy_summary": "模型策略",
                "channels": {"liepin": [], "xsaas": []},
                "strategy_v2": {
                    "step2_target_pool": [
                        {"path": "same_layer", "tier": "T2", "companies": [{"name": "某推断公司", "source": "llm_inferred", "confidence": "low"}], "rationale": "模型推断"}
                    ]
                },
            }

        self.service.llm = FakeLLM(fake_assessment(), search_strategy=_strategy)
        result = self.service.capability_runtime.run_search_strategy({"type": "job", "id": 90}, {"objective": "补充"})
        entry = next(
            entry
            for entry in result["strategy_v2"]["step2_target_pool"]
            if any(company["source"] == "llm_inferred" for company in entry["companies"])
        )
        assert "待确认" in entry["rationale"]

    def test_missing_graph_degrades_without_crash(self) -> None:
        (self.kb_dir / "kb_company_graph_jsj_v1.json").unlink()
        result = self._run_strategy(50)
        v2 = result["strategy_v2"]
        ok, errors = strategy_v2.validate_strategy_v2(v2)
        assert ok, errors
        assert v2["step2_source_distribution"].get("kb_graph", 0) == 0
        assert any("降级为空图谱" in line for line in v2["classification_trace"])

    def test_broken_graph_degrades_without_crash(self) -> None:
        (self.kb_dir / "kb_company_graph_jsj_v1.json").write_text("{这不是合法JSON", encoding="utf-8")
        result = self._run_strategy(50)
        v2 = result["strategy_v2"]
        assert v2["step2_source_distribution"].get("kb_graph", 0) == 0
        assert any("降级为空图谱" in line for line in v2["classification_trace"])


class StrategyRestrictedMergeTest(KbConsumptionDbCase):
    """禁挖名单/竞业并入 negative_rules（source=restricted_client）；restricted 键值不进 LLM 输入。"""

    def test_banned_companies_merge_into_negative_rules(self) -> None:
        result = self._run_strategy(80)
        v2 = result["strategy_v2"]
        banned = [rule for rule in v2["negative_rules"] if rule["source"] == "restricted_client"]
        assert banned, "restricted 白名单约束必须并入 negative_rules"
        banned_rule = next(rule for rule in banned if rule["type"] == "禁挖名单")
        assert "青岛芯恩" in banned_rule["rule"] and "福建晋华" in banned_rule["rule"]
        assert "在职保护" in banned_rule["rule"]
        assert any(rule["type"] == "竞业限制" and "某竞业对手甲" in rule["rule"] for rule in banned)
        assert any("restricted 约束" in line for line in v2["classification_trace"])
        ok, errors = strategy_v2.validate_strategy_v2(v2)
        assert ok, errors

    def test_restricted_values_never_enter_llm_payload(self) -> None:
        self._run_strategy(80)
        encoded = json.dumps(self.captured["payload"], ensure_ascii=False)
        for literal in _forbidden_literals():
            assert literal not in encoded, f"restricted 字面量不得进入生成上下文：{literal}"
        # 禁挖名单也只由运行时并入，不经 LLM 输入
        assert "青岛芯恩" not in encoded


class RestrictedLeakContractTest(KbConsumptionDbCase):
    """P0 契约：遍历 case fixture restricted 字面量，三条对外路径一律不得含。"""

    def _assert_no_leak(self, payload: object, path_label: str) -> None:
        encoded = json.dumps(payload, ensure_ascii=False)
        for literal in _forbidden_literals():
            assert literal not in encoded, f"restricted 泄漏（{path_label}）：{literal}"

    def test_contract_copilot_general_answer(self) -> None:
        result = self.service.copilot(
            "鹏新旭这个客户怎么样？费率多少？",
            session_id="s4-2-contract-copilot",
            context={"type": "global"},
        )
        self._assert_no_leak(result, "Copilot 通用回答")

    def test_contract_job_detail_api(self) -> None:
        self._prepare_core_tables()
        response = CoreService(self.db_path).job(80)
        assert response["ok"] is True
        self._assert_no_leak(response, "岗位详情 API")
        # 画像挂载键存在（未命中时 matched=False），且长川岗位挂上白名单画像
        assert response["job"]["client_profile"] == {"matched": False}
        changchuan = CoreService(self.db_path).job(50)
        assert changchuan["job"]["client_profile"]["matched"] is True
        assert "封测设备龙头" in changchuan["job"]["client_profile"]["context"]["selling_points"]
        self._assert_no_leak(changchuan, "岗位详情 API（长川）")

    def test_contract_strategy_artifact(self) -> None:
        result = self._run_strategy(80)
        artifact = result["artifacts"][0]
        self._assert_no_leak(
            {"content": artifact["content"], "metadata": artifact["metadata"]}, "策略 artifact"
        )
        # 禁挖名单只出现在 negative_rules 的内部约束区（source=restricted_client）
        v2 = artifact["metadata"]["strategy_v2"]
        holders = [rule for rule in v2["negative_rules"] if "青岛芯恩" in rule["rule"]]
        assert holders and all(rule["source"] == "restricted_client" for rule in holders)
        assert "青岛芯恩" in artifact["content"], "禁挖名单允许出现在策略约束区"
        plan_channels = json.dumps(artifact["metadata"]["plan"].get("channels", {}), ensure_ascii=False)
        assert "青岛芯恩" not in plan_channels, "禁挖名单不得进入渠道查询等执行面"


class ChangchuanIntegrationTest(KbConsumptionDbCase):
    """联调样本：长川科技岗位 → 自动挂画像 → 策略上下文含画像要点（注入前后对比）。"""

    def test_changchuan_end_to_end_before_after(self) -> None:
        # 注入后：KB fixture 就位
        after = self._run_strategy(50)
        after_payload = self.captured["payload"]

        # 注入前：KB 指向空目录
        empty_kb = Path(tempfile.mkdtemp()) / "empty_kb"
        empty_kb.mkdir(parents=True)
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(empty_kb)
        try:
            before = self._run_strategy(50)
            before_payload = self.captured["payload"]
        finally:
            os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(self.kb_dir)

        comparison = {
            "before": {
                "client_profile_matched": before_payload["client_profile"].get("matched"),
                "profile_matched": before["strategy_v2"]["profile_matched"],
                "kb_graph_candidates": len(before_payload.get("kb_graph_candidates") or []),
            },
            "after": {
                "client_profile_matched": after_payload["client_profile"].get("matched"),
                "match_rule": after_payload["client_profile"].get("rule"),
                "context_keys": sorted(after_payload["client_profile"].get("context", {})),
                "selling_points_head": after_payload["client_profile"]["context"]["selling_points"][:30],
                "profile_matched": after["strategy_v2"]["profile_matched"],
                "kb_graph_candidates": len(after_payload.get("kb_graph_candidates") or []),
            },
        }
        print("\n长川科技画像挂载注入前后对比：\n" + json.dumps(comparison, ensure_ascii=False, indent=2))

        assert comparison["before"]["client_profile_matched"] is False
        assert comparison["before"]["profile_matched"]["rule"] == "none"
        assert comparison["before"]["kb_graph_candidates"] == 0
        assert comparison["after"]["client_profile_matched"] is True
        assert comparison["after"]["match_rule"] == "exact"
        assert comparison["after"]["profile_matched"] == {
            "name": "长川科技", "rule": "exact", "needs_confirmation": False,
        }
        assert comparison["after"]["kb_graph_candidates"] >= 1

        # 岗位详情上下文同样自动挂载
        self._prepare_core_tables()
        detail = CoreService(self.db_path).job(50)
        assert detail["job"]["client_profile"]["matched"] is True
        assert detail["job"]["client_profile"]["rule"] == "exact"
        assert "封测设备龙头" in detail["job"]["client_profile"]["context"]["selling_points"]


if __name__ == "__main__":
    unittest.main()
