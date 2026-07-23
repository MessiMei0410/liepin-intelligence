"""S5-1：Mapping 直挖 —— mapping_task 数据模型 / 团队定位器 / 只读采集器 / 名单生成 / 触发路由测试。

口径：docs/TASKCARD_S5-1_mapping直挖_20260723.md（验收标准）+ PRD §2/§3/§7。
全部使用临时库 + 临时 KB fixture（运行时只读，绝不触碰真实知识库与生产 DB）；
采集器一律注入本地 fixture fetcher，绝不打外网。
覆盖：schema 必备键/版本校验、无 source_urls 人名写入被拒（硬锚点）、编造检查（全候选 ≥1 URL）、
禁挖公司候选过滤、团队定位器（图谱优先 + evidence 标注）、采集器失败记 stats 不静默
（超时/404/JS 壳页/页面数上限）、触发路由 200/404/幂等、restricted 不回泄。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from a_system_agent import mapping_task  # noqa: E402
from asa_core.app import create_app  # noqa: E402

# ---------------------------------------------------------------------------
# 公共 fixture
# ---------------------------------------------------------------------------

STRATEGY_V2 = {
    "schema_version": "strategy_v2",
    "input_level": "L1",
    "step1_job_essence": {"statement": "计算电源方向技术市场岗", "value_chain_role": "TME", "confirmed_by": "consultant"},
    "step2_target_pool": [
        {
            "path": "same_layer", "tier": "T1",
            "companies": [
                {"name": "晶丰明源", "source": "client_doc", "confidence": "high"},
                {"name": "杰华特", "source": "client_doc", "confidence": "high"},
                {"name": "未知半导体", "source": "llm_inferred", "confidence": "low"},
            ],
            "rationale": "同层友商",
        },
        {
            "path": "reverse", "tier": "T2",
            "companies": [{"name": "联想", "source": "client_doc", "confidence": "high"}],
            "rationale": "客户整机厂",
        },
    ],
    "step3_level_mapping": {"accepted_levels": ["经理", "总监"], "calibration_rule": "按职责定档"},
    "step4_keyword_groups": [{"group": "core", "targets": "T1 友商", "terms": ["多相控制器", "DrMOS"]}],
    "step5_expectation": {"expected_recall_per_tier": {"T1": 10}, "fallback_plan": ""},
    "negative_rules": [],
    "consultant_edits": [],
    "archetype_id": "tme_s5",
}

SEED_FIXTURE = {
    "meta": {"created": "2026-07-01"},
    "job_archetype": {
        "archetype_id": "tme_s5",
        "title": "技术市场经理（计算电源）",
        "client": "士兰微",
        "essence": "计算电源方向技术市场岗",
        "directions": [{"name": "PC", "products": ["多相控制器", "DrMOS"], "competitors": ["杰华特"]}],
        "target_functions": ["TME", "FAE"],
        "location_policy": "杭州优先",
    },
    "target_company_pool": {
        "T1_competitor_device": {
            "rationale": "同层友商",
            "companies": [
                {"name": "杰华特", "tier": "T1", "directions": ["PC", "服务器"]},
                {"name": "晶丰明源", "tier": "T1", "directions": ["服务器"]},
            ],
        },
    },
    "keyword_groups": [],
    "negative_rules": [],
    "level_mapping": {},
}

GRAPH_FIXTURE = {
    "companies": {
        "晶丰明源半导体（上海）股份有限公司": {
            "track": "模拟芯片",
            "business": "电源管理芯片",
            "categories": ["设计公司"],
        }
    }
}

CASE_FIXTURE = {
    "meta": {"case_id": "case_silan_s5"},
    "client_profile": {"name": "士兰微"},
    "restricted": {
        "banned_companies": ["杰华特"],
        "consultant_phone": "13912345678",
        "fee_rate": "费率23%",
        "scripts_redline": "话术红线MARKER_REDLINE_S5",
    },
}

FORBIDDEN_LITERALS = ["13912345678", "费率23%", "MARKER_REDLINE_S5", "话术红线"]

ARCHETYPE_NORMALIZED = {
    "archetype_id": "tme_s5",
    "title": "技术市场经理（计算电源）",
    "client": "士兰微",
    "essence": "计算电源方向技术市场岗",
    "directions": [{"name": "PC", "products": ["多相控制器", "DrMOS"], "competitors": ["杰华特"]}],
    "target_functions": ["TME", "FAE"],
    "location_policy": "杭州优先",
    "target_company_pool": SEED_FIXTURE["target_company_pool"],
    "keyword_groups": [],
    "negative_rules": [],
    "level_mapping": {},
    "source_file": "seed_s5_tme_v1.json",
}

GRAPH_NORMALIZED = {
    "晶丰明源半导体（上海）股份有限公司": {
        "track": "模拟芯片",
        "business": "电源管理芯片",
        "categories": ["设计公司"],
    }
}

# 采集器 HTML fixture（本地，不打外网）
SITE_HTML = (
    "<html><head><title>杰华特加入我们</title></head><body>"
    "<h1>加入杰华特</h1><p>" + "我们专注于电源管理芯片，覆盖计算与服务器电源方向。" * 12 + "</p>"
    '<a href="/jobs/tme-power">技术市场经理（计算电源）</a>'
    '<a href="/jobs/fae-power">FAE（电源方向）</a>'
    "</body></html>"
)
JD_HTML = (
    "<html><head><title>技术市场经理（计算电源）</title></head><body>"
    "<h1>技术市场经理（计算电源）</h1><p>" + "负责多相控制器与 DrMOS 产品定义与客户导入。" * 12 + "</p>"
    "<p>工作地点：杭州</p><p>联系人：张三</p>"
    "</body></html>"
)
JS_SHELL_HTML = (
    '<html><head><title>App</title></head><body><div id="app"></div>'
    '<script>window.__INITIAL_STATE__={};</script></body></html>'
)
PATENT_JSON = json.dumps(
    {
        "results": {
            "cluster": [
                {
                    "result": [
                        {
                            "patent": {
                                "publication_number": "CN111000111A",
                                "title": "一种多相控制器",
                                "assignee": "杰华特微电子股份有限公司",
                                "inventor": ["李四", "王五"],
                            }
                        },
                        {
                            "patent": {
                                "publication_number": "CN222000222A",
                                "title": "无关公司专利",
                                "assignee": "无关电子有限公司",
                                "inventor": ["赵六"],
                            }
                        },
                    ]
                }
            ]
        }
    },
    ensure_ascii=False,
)


def _write_kb(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    (base / "seed_s5_tme_v1.json").write_text(json.dumps(SEED_FIXTURE, ensure_ascii=False), encoding="utf-8")
    (base / "kb_company_graph_jsj_v1.json").write_text(json.dumps(GRAPH_FIXTURE, ensure_ascii=False), encoding="utf-8")
    (base / "cases").mkdir(parents=True, exist_ok=True)
    (base / "cases" / "case_silan_s5_v1.json").write_text(json.dumps(CASE_FIXTURE, ensure_ascii=False), encoding="utf-8")
    return base


class KbCase(unittest.TestCase):
    """临时 KB 目录 + ASA_KNOWLEDGE_BASE_DIR 环境变量隔离。"""

    def setUp(self) -> None:
        self.kb_temp = tempfile.TemporaryDirectory()
        kb_dir = _write_kb(Path(self.kb_temp.name) / "kb")
        self.kb_dir = kb_dir
        self._old_kb_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(kb_dir)

    def tearDown(self) -> None:
        if self._old_kb_env is None:
            os.environ.pop("ASA_KNOWLEDGE_BASE_DIR", None)
        else:
            os.environ["ASA_KNOWLEDGE_BASE_DIR"] = self._old_kb_env
        self.kb_temp.cleanup()


def _valid_doc(**overrides) -> dict:
    doc = {
        "schema_version": "mapping_v1",
        "trigger": "manual",
        "job_id": 154,
        "strategy_ref": "artifact_strategy_1",
        "target_teams": [
            {
                "company": "杰华特",
                "team": "PC/服务器 方向 TME/FAE 团队",
                "location": "杭州",
                "evidence": [{"type": "图谱", "ref": "seed#target_company_pool:杰华特", "as_of": "2026-07-23"}],
                "confidence": "medium",
            }
        ],
        "candidates": [
            {
                "name": "李**",
                "current_role": "杰华特 研发（专利发明人）",
                "team_ref": 0,
                "source_urls": ["https://patents.google.com/patent/CN111000111A/zh"],
                "confidence": "medium",
                "reason": "公开专利发明人",
                "status": "pending",
                "consultant_note": "",
            }
        ],
        "stats": {"teams": 1, "candidates": 1, "confirmed": 0, "intaken": 0},
    }
    doc.update(overrides)
    return doc


# ---------------------------------------------------------------------------
# 1. schema 校验（必备键/版本）
# ---------------------------------------------------------------------------

class SchemaValidationTest(unittest.TestCase):
    def test_valid_doc_passes(self) -> None:
        assert mapping_task.validate_mapping_task(_valid_doc()) == []

    def test_version_and_required_keys(self) -> None:
        doc = _valid_doc(schema_version="mapping_v0")
        assert any("schema_version" in error for error in mapping_task.validate_mapping_task(doc))
        for key in mapping_task.REQUIRED_KEYS:
            doc = _valid_doc()
            doc.pop(key)
            errors = mapping_task.validate_mapping_task(doc)
            assert any(key in error for error in errors), f"缺键 {key} 必须报错：{errors}"

    def test_team_evidence_and_enums(self) -> None:
        doc = _valid_doc()
        doc["target_teams"] = [dict(_valid_doc()["target_teams"][0], evidence=[])]
        assert any("evidence" in error for error in mapping_task.validate_mapping_task(doc))
        doc = _valid_doc()
        doc["target_teams"] = [dict(_valid_doc()["target_teams"][0], confidence="sure")]
        assert any("confidence" in error for error in mapping_task.validate_mapping_task(doc))
        doc = _valid_doc(trigger="auto_send")
        assert any("trigger" in error for error in mapping_task.validate_mapping_task(doc))
        doc = _valid_doc()
        doc["candidates"] = [dict(_valid_doc()["candidates"][0], status="auto_contacted")]
        assert any("status" in error for error in mapping_task.validate_mapping_task(doc))


# ---------------------------------------------------------------------------
# 2. 无 source_urls 人名写入被拒（硬锚点）+ 编造检查
# ---------------------------------------------------------------------------

class NoSourceRejectedTest(unittest.TestCase):
    ARTIFACTS_DDL = """
    CREATE TABLE agent_artifacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        artifact_id TEXT NOT NULL UNIQUE,
        goal_id TEXT NOT NULL,
        workflow_id TEXT NOT NULL,
        step_id INTEGER,
        artifact_type TEXT NOT NULL,
        title TEXT NOT NULL,
        mime_type TEXT NOT NULL DEFAULT 'text/markdown',
        file_path TEXT,
        content TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        validation_status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    );
    """

    def test_validate_rejects_empty_source_urls(self) -> None:
        doc = _valid_doc()
        doc["candidates"] = [dict(doc["candidates"][0], source_urls=[])]
        errors = mapping_task.validate_mapping_task(doc)
        assert any("source_urls" in error for error in errors), errors
        doc["candidates"] = [dict(doc["candidates"][0], source_urls=["  ", ""])]
        assert any("source_urls" in error for error in mapping_task.validate_mapping_task(doc))

    def test_upsert_refuses_whole_doc_when_candidate_has_no_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            conn = sqlite3.connect(Path(temp) / "m.db")
            conn.row_factory = sqlite3.Row
            conn.executescript(self.ARTIFACTS_DDL)
            doc = _valid_doc(workflow_id="wf-x", goal_id="goal-x")
            doc["candidates"] = [dict(doc["candidates"][0], source_urls=[])]
            with self.assertRaises(ValueError):
                mapping_task.upsert_mapping_task(conn, doc)
            count = conn.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0]
            assert count == 0, "校验不过必须整条拒写，不得落任何 artifact"
            conn.close()

    def test_build_candidates_drops_no_source_and_fabrication_check(self) -> None:
        clues = [
            {"company": "杰华特", "name": "李四", "source_url": "https://patents.google.com/patent/CN111000111A/zh",
             "confidence": "medium", "reason": "专利发明人", "team_ref": 0},
            {"company": "杰华特", "name": "编造甲", "source_url": "", "confidence": "low", "reason": "", "team_ref": 0},
            {"company": "杰华特", "name": "编造乙", "confidence": "low", "reason": "", "team_ref": 0},
        ]
        candidates, stats = mapping_task.build_candidates(clues, banned=[])
        assert stats["rejected_no_source"] == 2, stats
        assert len(candidates) == 1, "无来源人名不得进名单"
        # 编造检查：名单内每个候选必须能回指 ≥1 个具体来源 URL
        for candidate in candidates:
            urls = [u for u in candidate.get("source_urls") or [] if str(u).strip()]
            assert urls, f"候选缺来源 URL：{candidate}"
            assert all(u.startswith("http") for u in urls)
        # 掩码口径：姓氏+**
        assert candidates[0]["name"] == "李**"

    def test_upsert_idempotent_version_bump(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            conn = sqlite3.connect(Path(temp) / "m.db")
            conn.row_factory = sqlite3.Row
            conn.executescript(self.ARTIFACTS_DDL)
            doc = _valid_doc(workflow_id="wf-x", goal_id="goal-x")
            first = mapping_task.upsert_mapping_task(conn, doc)
            second = mapping_task.upsert_mapping_task(conn, dict(doc))
            assert first == second, "同工作流重算覆盖同一 artifact"
            row = conn.execute("SELECT COUNT(*) AS c FROM agent_artifacts").fetchone()
            assert row["c"] == 1
            payload = mapping_task.get_mapping_task(conn, first)
            assert payload["mapping_task"]["version"] == 2
            assert len(payload["mapping_task"]["history"]) == 1
            conn.close()


# ---------------------------------------------------------------------------
# 3. 禁挖名单过滤（含 restricted 集成 + 不回泄）
# ---------------------------------------------------------------------------

class BannedFilterTest(KbCase):
    def test_banned_company_candidates_filtered(self) -> None:
        clues = [
            {"company": "杰华特", "name": "李四", "source_url": "https://patents.google.com/patent/CN111/zh",
             "confidence": "medium", "reason": "专利发明人", "team_ref": 0},
            {"company": "杰华特微电子股份有限公司", "name": "王五", "source_url": "https://patents.google.com/patent/CN222/zh",
             "confidence": "medium", "reason": "专利发明人（禁挖公司别名）", "team_ref": 0},
            {"company": "晶丰明源", "name": "赵六", "source_url": "https://patents.google.com/patent/CN333/zh",
             "confidence": "medium", "reason": "专利发明人", "team_ref": 1},
        ]
        candidates, stats = mapping_task.build_candidates(clues, banned=["杰华特"])
        assert stats["banned_filtered"] == 2, "禁挖公司（含别名）的人不进名单"
        assert len(candidates) == 1

    def test_build_mapping_task_uses_restricted_whitelist(self) -> None:
        class _ClueCollector:
            def collect_company(self, company, **_kwargs):
                return {
                    "evidence": [],
                    "clues": [
                        {"kind": "专利", "company": company, "name": "李四",
                         "current_role": f"{company} 研发", "source_url": "https://patents.google.com/patent/CN111/zh",
                         "confidence": "medium", "reason": "测试线索"}
                    ],
                    "failures": [],
                    "pages_fetched": 0,
                    "location": "",
                }

        doc = mapping_task.build_mapping_task(
            job_id=154, trigger="manual", strategy_ref="artifact_strategy_1",
            strategy_doc=STRATEGY_V2, client="士兰微", job_title="技术市场经理/总监（PC电源）",
            graph=GRAPH_NORMALIZED, archetype=ARCHETYPE_NORMALIZED, collector=_ClueCollector(),
        )
        assert doc["stats"]["banned_filtered"] == 1, "杰华特为禁挖公司，其候选必须被过滤"
        companies = {
            doc["target_teams"][c["team_ref"]]["company"] for c in doc["candidates"]
        }
        assert "杰华特" not in companies
        # restricted 层只白名单出库：费率/手机号/话术红线不进任何输出
        encoded = json.dumps(doc, ensure_ascii=False)
        for literal in FORBIDDEN_LITERALS:
            assert literal not in encoded, f"restricted 字面量不得出现在 mapping_task：{literal}"


# ---------------------------------------------------------------------------
# 4. 团队定位器（图谱优先 + evidence 标注）
# ---------------------------------------------------------------------------

class TeamLocatorTest(unittest.TestCase):
    def test_graph_first_and_seed_evidence(self) -> None:
        teams, trace = mapping_task.locate_target_teams(
            STRATEGY_V2, graph=GRAPH_NORMALIZED, archetype=ARCHETYPE_NORMALIZED, as_of="2026-07-23"
        )
        assert len(teams) == 4, "T1 三家 + T2 联想（含未知公司降级保留）"
        by_company = {team["company"]: team for team in teams}
        # 图谱命中：晶丰明源（规范化别名命中图谱全名），证据 type=图谱 ref 指图谱条目
        jfm = by_company["晶丰明源"]
        graph_refs = [e for e in jfm["evidence"] if e["type"] == "图谱" and e["ref"].startswith("kb_company_graph:")]
        assert graph_refs, f"图谱已有信息必须优先复用：{jfm['evidence']}"
        assert graph_refs[0]["as_of"] == "2026-07-23"
        # 种子方向标注：杰华特未进图谱，用原型 directions 定位团队
        jht = by_company["杰华特"]
        assert "PC" in jht["team"] and "TME" in jht["team"]
        assert any("#target_company_pool" in e["ref"] for e in jht["evidence"])
        # 未知公司：不编造，降级 low + 兜底证据 + notes 说明
        unknown = by_company["未知半导体"]
        assert unknown["confidence"] == "low"
        assert unknown["notes"], "无图谱/种子覆盖的公司必须留痕说明"
        assert unknown["evidence"][0]["ref"] == "strategy_v2:step2_target_pool"
        # 每条团队证据三要素齐全
        for team in teams:
            for item in team["evidence"]:
                assert item["type"] in mapping_task.EVIDENCE_TYPES
                assert item["ref"].strip() and item["as_of"].strip()
        assert any("团队定位" in line for line in trace)

    def test_locator_without_graph_and_archetype_degrades(self) -> None:
        teams, _trace = mapping_task.locate_target_teams(STRATEGY_V2, graph={}, archetype=None)
        assert len(teams) == 4
        assert all(team["confidence"] == "low" for team in teams)
        assert all(team["evidence"] for team in teams), "降级也必须带兜底证据，不留空 evidence"


# ---------------------------------------------------------------------------
# 5. 采集器（本地 fixture fetcher；失败记 stats 不静默）
# ---------------------------------------------------------------------------

class FixtureFetcher:
    """本地 fixture fetcher：按 URL 前缀路由到固定响应，绝不打外网。"""

    def __init__(self, routes: dict[str, tuple[int, str, str]]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float) -> tuple[int, str, str]:
        self.calls.append(url)
        for prefix, response in self.routes.items():
            if url.startswith(prefix):
                return response
        return (0, "", "network_error")


class CollectorTest(unittest.TestCase):
    def test_success_site_jd_contact_and_location(self) -> None:
        fetcher = FixtureFetcher(
            {
                "https://example.com/careers": (200, SITE_HTML, ""),
                "https://example.com/jobs/tme-power": (200, JD_HTML, ""),
                "https://example.com/jobs/fae-power": (200, JD_HTML, ""),
                "https://patents.google.com/xhr/query": (200, PATENT_JSON, ""),
                "https://api.openalex.org/works": (200, '{"results": []}', ""),
            }
        )
        collector = mapping_task.MappingCollector(fetcher=fetcher)
        result = collector.collect_company(
            "杰华特", keywords=["多相控制器"], site_hint="https://example.com/careers", patent_key="杰华特"
        )
        types = {item["type"] for item in result["evidence"]}
        assert "官网" in types and "招聘JD" in types
        assert result["location"] == "杭州"
        contacts = [c for c in result["clues"] if c["kind"] == "招聘JD"]
        assert any(c["name"] == "张三" and c["source_url"].endswith("/jobs/tme-power") for c in contacts)
        patents = [c for c in result["clues"] if c["kind"] == "专利"]
        # 申请人单位对不上的（无关电子）不取
        assert {c["name"] for c in patents} == {"李四", "王五"}
        assert all(c["source_url"].startswith("https://patents.google.com/patent/") for c in patents)
        assert result["failures"] == []

    def test_timeout_404_js_shell_recorded_not_silent(self) -> None:
        fetcher = FixtureFetcher(
            {
                "https://timeout.example.com": (0, "", "timeout"),
                "https://404.example.com": (404, "", "http_404"),
                "https://js.example.com": (200, JS_SHELL_HTML, ""),
            }
        )
        collector = mapping_task.MappingCollector(fetcher=fetcher)
        timeout_result = collector.collect_company("甲公司", site_hint="https://timeout.example.com")
        assert any(f["reason"] == "timeout" for f in timeout_result["failures"])
        not_found = collector.collect_company("乙公司", site_hint="https://404.example.com/jobs")
        assert any(f["reason"] == "http_404" for f in not_found["failures"])
        js_shell = collector.collect_company("丙公司", site_hint="https://js.example.com/careers")
        assert any(f["reason"] == "js_shell" for f in js_shell["failures"])
        assert js_shell["evidence"] == [], "JS 壳页不得当作有效证据"

    def test_page_cap_and_patent_parse_error(self) -> None:
        many_links = (
            "<html><body><p>" + "招聘信息汇总页面，岗位众多。" * 30 + "</p>"
            + "".join(f'<a href="/jobs/{i}">职位{i}</a>' for i in range(10))
            + "</body></html>"
        )
        fetcher = FixtureFetcher(
            {
                "https://cap.example.com/careers": (200, many_links, ""),
                "https://cap.example.com/jobs/": (200, JD_HTML, ""),
                "https://patents.google.com/xhr/query": (200, "<html>不是JSON</html>", ""),
            }
        )
        collector = mapping_task.MappingCollector(fetcher=fetcher, max_pages=3)
        result = collector.collect_company("丁公司", site_hint="https://cap.example.com/careers")
        assert result["pages_fetched"] <= 3, "每公司页面数必须有小上限"
        assert any(f["reason"] == "parse_error" for f in result["failures"]), "检索结构变动必须记 parse_error"

    def test_maimai_reserved_only(self) -> None:
        stub = mapping_task.MaimaiCollector()
        result = stub.collect_company("杰华特")
        assert result["clues"] == [] and result["failures"] == []
        assert "预留" in result["note"]

    OPENALEX_JSON = json.dumps(
        {
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "display_name": "A multiphase controller design",
                    "doi": "https://doi.org/10.1109/example.1",
                    "authorships": [
                        {"author": {"display_name": "Alice Chen"}, "raw_affiliation_strings": ["Silergy Corporation, Hangzhou"]},
                        {"author": {"display_name": "Bob Li"}, "raw_affiliation_strings": ["Zhejiang University"]},
                    ],
                },
                {
                    "id": "https://openalex.org/W2",
                    "display_name": "Unrelated work",
                    "doi": "https://doi.org/10.1109/example.2",
                    "authorships": [
                        {"author": {"display_name": "Carol Wang"}, "raw_affiliation_strings": ["Tsinghua University"]},
                    ],
                },
            ]
        },
        ensure_ascii=False,
    )

    def test_paper_openalex_authors_conservative(self) -> None:
        fetcher = FixtureFetcher({"https://api.openalex.org/works": (200, self.OPENALEX_JSON, "")})
        collector = mapping_task.MappingCollector(fetcher=fetcher)
        result = collector.collect_company("矽力杰", keywords=["多相控制器"], paper_key="Silergy")
        papers = [c for c in result["clues"] if c["kind"] == "论文"]
        # 只取单位标注含公司的作者：Alice 命中，Bob（浙大）/Carol（清华）不取
        assert [c["name"] for c in papers] == ["Alice Chen"]
        assert papers[0]["source_url"] == "https://doi.org/10.1109/example.1"
        assert "Silergy" in papers[0]["reason"]
        assert papers[0]["confidence"] == "medium"

    def test_shared_source_circuit_breaker(self) -> None:
        # 专利源全局不可达：首公司失败后熔断，后续公司跳过并留痕（不重复打满超时）
        fetcher = FixtureFetcher({"https://patents.google.com/xhr/query": (0, "", "timeout")})
        collector = mapping_task.MappingCollector(fetcher=fetcher)
        first = collector.collect_company("甲公司", keywords=["电源"])
        assert any(f["reason"] == "timeout" for f in first["failures"])
        patent_calls = [c for c in fetcher.calls if "patents.google.com" in c]
        assert len(patent_calls) == 1, "熔断后不得再请求专利源"
        second = collector.collect_company("乙公司", keywords=["电源"])
        assert any(f["reason"] == "skipped_after_failure" for f in second["failures"])
        assert len([c for c in fetcher.calls if "patents.google.com" in c]) == 1

    def test_career_link_no_substring_false_positive(self) -> None:
        # synchronous-rectifiers / achron 之类子串不得被误判为招聘链接（hr/join 误命中回归）
        page = (
            "<html><body><p>" + "公司产品导航页面。" * 30 + "</p>"
            '<a href="/en/products/ac-dc/synchronous-rectifiers.html">产品</a>'
            '<a href="/en/design-tools/reference-design-partners/achron">合作</a>'
            '<a href="/en/about-mps/careers.html">加入我们</a>'
            "</body></html>"
        )
        careers = "<html><body><p>" + "招聘岗位列表。" * 30 + "</p></body></html>"
        fetcher = FixtureFetcher(
            {
                "https://example.com": (200, page, ""),
                "https://example.com/en/about-mps/careers.html": (200, careers, ""),
            }
        )
        collector = mapping_task.MappingCollector(fetcher=fetcher)
        result = collector.collect_company("MPS", site_hint="https://example.com")
        assert not any("synchronous-rectifiers" in c for c in fetcher.calls), fetcher.calls
        assert not any("achron" in c for c in fetcher.calls), fetcher.calls
        assert any("careers.html" in c for c in fetcher.calls)
        assert any(e["type"] == "招聘JD" for e in result["evidence"])


# ---------------------------------------------------------------------------
# 6. 触发路由 200/404/幂等（POST/GET /api/v1/jobs/{id}/mapping-tasks）
# ---------------------------------------------------------------------------

class MappingTaskApiTest(KbCase):
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
        conn.execute("INSERT INTO clients VALUES (1,'士兰微')")
        conn.execute("INSERT INTO jobs VALUES (154,1,'技术市场经理/总监（PC电源）')")
        conn.execute("INSERT INTO jobs VALUES (155,1,'无策略岗位')")
        conn.commit()
        conn.close()

    def _seed_workflows(self, db_path: Path) -> None:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            INSERT INTO agent_goals (goal_id,objective,title,context_type,context_id,context_json,status)
            VALUES ('goal_154','给士兰微补充人选','寻访','job',154,'{"type":"job","id":154}','blocked')
            """
        )
        conn.execute(
            "INSERT INTO agent_workflows (workflow_id,goal_id,status) VALUES ('wf-154','goal_154','blocked')"
        )
        conn.execute(
            """
            INSERT INTO agent_artifacts
            (artifact_id,goal_id,workflow_id,step_id,artifact_type,title,mime_type,content,metadata_json,validation_status)
            VALUES ('artifact_strategy_154','goal_154','wf-154',NULL,'search_strategy','多渠道寻访策略','text/markdown','# 策略',?,'passed')
            """,
            (json.dumps({"strategy_v2": STRATEGY_V2}, ensure_ascii=False),),
        )
        conn.execute(
            """
            INSERT INTO agent_goals (goal_id,objective,title,context_type,context_id,context_json,status)
            VALUES ('goal_155','寻访','寻访','job',155,'{"type":"job","id":155}','blocked')
            """
        )
        conn.execute(
            "INSERT INTO agent_workflows (workflow_id,goal_id,status) VALUES ('wf-155','goal_155','blocked')"
        )
        conn.commit()
        conn.close()

    def test_post_get_idempotent_and_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "asa.db"
            self._create_db(db_path)
            app = create_app(db_path=db_path, start_legacy=False)
            self._seed_workflows(db_path)

            class _ClueCollector:
                def collect_company(self, company, **_kwargs):
                    return {
                        "evidence": [{"type": "官网", "ref": "https://example.com/careers", "as_of": "2026-07-23"}],
                        "clues": [
                            {"kind": "专利", "company": company, "name": "李四",
                             "current_role": f"{company} 研发（专利发明人）",
                             "source_url": "https://patents.google.com/patent/CN111/zh",
                             "confidence": "medium", "reason": "测试线索"},
                        ],
                        "failures": [],
                        "pages_fetched": 1,
                        "location": "杭州",
                    }

            with TestClient(app) as client, mock.patch.object(mapping_task, "MappingCollector", _ClueCollector):
                missing = client.post(
                    "/api/v1/jobs/999/mapping-tasks",
                    json={"request_id": "req-m1", "trigger": "manual"},
                    headers={"Idempotency-Key": "k-m1"},
                )
                assert missing.status_code == 404, missing.text

                no_strategy = client.post(
                    "/api/v1/jobs/155/mapping-tasks",
                    json={"request_id": "req-m2", "trigger": "manual"},
                    headers={"Idempotency-Key": "k-m2"},
                )
                assert no_strategy.status_code == 409, "无 strategy_v2 策略不得发起 Mapping"

                bad_trigger = client.post(
                    "/api/v1/jobs/154/mapping-tasks",
                    json={"request_id": "req-m3", "trigger": "auto"},
                    headers={"Idempotency-Key": "k-m3"},
                )
                assert bad_trigger.status_code == 409

                first = client.post(
                    "/api/v1/jobs/154/mapping-tasks",
                    json={"request_id": "req-154-1", "trigger": "decision_tree_exhausted"},
                    headers={"Idempotency-Key": "k-154-1"},
                )
                assert first.status_code == 200, first.text
                payload = first.json()
                assert payload["ok"] is True and payload["artifact_id"]
                assert payload["receipt"]["idempotent_replay"] is False
                doc = payload["mapping_task"]
                assert doc["schema_version"] == "mapping_v1"
                assert doc["trigger"] == "decision_tree_exhausted"
                assert doc["strategy_ref"] == "artifact_strategy_154"
                assert doc["stats"]["teams"] == len(doc["target_teams"])
                assert doc["stats"]["candidates"] == len(doc["candidates"])
                # 编造检查：API 返回的全部候选必须带 ≥1 来源 URL，且姓名遮罩
                for candidate in doc["candidates"]:
                    assert candidate["source_urls"], candidate
                    assert "*" in candidate["name"]
                # 禁挖：杰华特候选不得出现
                companies = {doc["target_teams"][c["team_ref"]]["company"] for c in doc["candidates"]}
                assert "杰华特" not in companies
                assert doc["stats"]["banned_filtered"] >= 1
                # restricted 不回泄
                encoded = json.dumps(payload, ensure_ascii=False)
                for literal in FORBIDDEN_LITERALS:
                    assert literal not in encoded, literal

                replay = client.post(
                    "/api/v1/jobs/154/mapping-tasks",
                    json={"request_id": "req-154-1", "trigger": "decision_tree_exhausted"},
                    headers={"Idempotency-Key": "k-154-1"},
                )
                assert replay.status_code == 200
                assert replay.json()["receipt"]["idempotent_replay"] is True, "同键重放首次响应"

                got = client.get(f"/api/v1/jobs/154/mapping-tasks/{payload['artifact_id']}")
                assert got.status_code == 200, got.text
                detail = got.json()
                assert detail["ok"] is True and detail["mapping_task"]["job_id"] == 154
                assert detail["mapping_task"]["stats"]["teams"] >= 1

                wrong_job = client.get(f"/api/v1/jobs/155/mapping-tasks/{payload['artifact_id']}")
                assert wrong_job.status_code == 404, "artifact 不属于该岗位 → 404"
                wrong_artifact = client.get("/api/v1/jobs/154/mapping-tasks/mapping_task_nope")
                assert wrong_artifact.status_code == 404

                # job 时间线事件：candidate_events 落 mapping_task_created
                conn = sqlite3.connect(db_path)
                events = conn.execute(
                    "SELECT event_type,summary,source_id FROM candidate_events WHERE job_id=154 AND event_type='mapping_task_created'"
                ).fetchall()
                conn.close()
                assert len(events) == 1, "创建 Mapping 任务卡必须写 job 时间线（幂等重放不重复写）"
                assert events[0][2] == payload["artifact_id"]


if __name__ == "__main__":
    unittest.main()
