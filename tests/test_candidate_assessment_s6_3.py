"""S6-3：判人评估器 —— 风险点维度（risks）+ 推荐报告强制引用评估块测试。

口径：docs/ASA_PRD_S6_判人评估器_2026-07-23.md §2（风险点）/§1 ②（报告强制引用）/§5（S6-3 行）。
全部使用临时库 + 临时 KB fixture + FakeLLM + stub 采集器，绝不触碰生产 DB、真实知识库与外网。

覆盖：
1. 确定性检出：gap 空窗（>6 个月，>12 升 high）/ 频繁跳动（短任期段数、近 5 年窗口、稳定岗升档）/
   时间线冲突（重叠 >6 个月，过度包装信号类）/ 硬条件差距（学历/年限 high、技能缺项 low 无证据）；
   学历段（"本科 2009.09-2013.06"）不计入工作经历。
2. LLM 语义项：title 通胀/过度包装 kind 白名单；证据逐字闸（编造引用整条丢弃）；
   kind 越权丢弃；LLM 失败降级纯确定性（记 fallback，不阻断）。
3. 空态：无风险项 items=[] + "未见需核实的问题"，confidence 按 checks_run 定；
   severity 枚举校验（validate 层）。
4. 敏感扫描复用：LLM risk 文本命中敏感词 → 拒写 + 扫描日志；决策禁语 → 拒写；
   简历逐字证据含敏感词 → 剥离（LLM 项证据归零整条丢弃）。
5. 报告引用块：report_reference_block 四行结构/top3 按 severity 排序/旧版评估 risks_pending。
6. 主流程集成：五类一次跑出 + version=s6-3-v1 + validate 通过 + markdown 含「需要核实的问题」。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from a_system_agent import assessment_signals, candidate_assessment  # noqa: E402
from a_system_agent.llm import FakeLLM  # noqa: E402
from a_system_agent.schema import ensure_schema  # noqa: E402

TODAY = date(2026, 7, 24)

CLEAN_RESUME = (
    "张** 求职期望：杭州 技术市场经理\n"
    "2021.03-至今 杰华特微电子股份有限公司 · 技术市场经理 负责PC电源多相控制器产品线市场推广\n"
    "2017.06-2021.02 晶丰明源半导体（上海）股份有限公司 · FAE工程师 负责AC-DC电源芯片客户支持\n"
    "2013.07-2017.05 立讯精密 · 硬件工程师 负责消费电子硬件电路设计\n"
    "浙江大学 · 电子科学与技术 · 本科 2009.09-2013.06"
)

# 风险样本：一段 8 个月空窗 + 两段短任期 + 大专学历（配硕士/年限硬条件用）
RISKY_RESUME = (
    "邓** 求职期望：杭州 技术市场总监\n"
    "2024.01-至今 某半导体公司 · 技术市场总监 负责PC电源产品推广\n"
    "2022.05-2022.11 甲科技公司 · 销售工程师 负责客户跟进\n"
    "2020.03-2021.08 乙电子公司 · FAE工程师 负责电源芯片客户支持\n"
    "2018.07-2019.02 丙半导体公司 · 工程师 负责产品测试\n"
    "某学院 · 电子信息 · 大专 2015.09-2018.06"
)

GRAPH_FIXTURE = {
    "companies": {
        "杰华特微电子股份有限公司": {"track": "模拟芯片", "business": "电源管理芯片 多相控制器", "categories": ["设计公司"]},
    }
}

GOOD_LLM = {
    "trajectory": {
        "verdict": "技术市场线一路上行",
        "segments": [
            {"company": "杰华特微电子股份有限公司", "title": "技术市场经理", "period": "2021.03-至今",
             "tier": "T1", "tier_source": "graph", "team": "", "report_line": "", "note": "PC电源产品线"},
        ],
        "promotion_pace": "fast",
        "tech_evolution": "rising",
        "evidence": [{"type": "简历", "ref": "2021.03-至今 杰华特微电子股份有限公司 · 技术市场经理"}],
        "confidence": "certain",
    },
    "move_history": {
        "verdict": "跳槽均为上升",
        "moves": [
            {"from": "晶丰明源", "to": "杰华特", "direction": "up", "platform": "up",
             "title_direction": "up", "responsibility_direction": "up", "reason": "FAE 转技术市场"},
        ],
        "current_move": "lateral",
        "evidence": [{"type": "简历", "ref": "2017.06-2021.02 晶丰明源半导体（上海）股份有限公司 · FAE工程师"}],
        "confidence": "certain",
    },
    "consultant_summary": "轨迹清晰，两次跳槽均上行。",
}

GOOD_PM = {
    "percentile": {"verdict": "同方向同龄人中处于靠前位置", "evidence": [], "confidence": "certain"},
    "motivation": {"verdict": "在职时长已超其历史平均任期", "evidence": [], "confidence": "certain"},
}

GOOD_RISKS_LLM = {
    "items": [
        {
            "kind": "title_inflation",
            "risk": "title 为技术市场总监但职责描述仅为产品推广执行，未见团队管理，需要核实实际汇报线与带人情况",
            "severity": "medium",
            "evidence": [{"type": "简历", "ref": "2024.01-至今 某半导体公司 · 技术市场总监 负责PC电源产品推广"}],
        }
    ]
}

API_SCHEMA = """
CREATE TABLE clients(id INTEGER PRIMARY KEY,name TEXT);
CREATE TABLE jobs(id INTEGER PRIMARY KEY,client_id INTEGER,title TEXT,summary TEXT,hard_requirements TEXT);
CREATE TABLE positions(id INTEGER PRIMARY KEY,client TEXT,title TEXT);
CREATE TABLE people(id INTEGER PRIMARY KEY,display_name TEXT,current_company TEXT,current_title TEXT,
  city TEXT,education TEXT,experience TEXT);
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


def _stub_fetcher(url: str, timeout: float) -> tuple[int, str, str]:
    return (0, "", "network_error")


def _create_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(API_SCHEMA)
        ensure_schema(conn)
        conn.execute("INSERT INTO clients VALUES (1,'士兰微'),(2,'长越科技')")
        conn.execute("INSERT INTO jobs VALUES (154,1,'技术市场经理/总监（PC电源）','PC电源技术市场','5年以上电源芯片经验')")
        conn.execute(
            "INSERT INTO jobs VALUES (155,1,'高级模拟设计工程师','模拟IC设计','硕士及以上学历，8年以上电源芯片经验，熟悉多相控制器')"
        )
        conn.commit()
    finally:
        conn.close()


def _seed_person(
    db_path: Path,
    *,
    candidate_id: int,
    job_id: int,
    person_id: int,
    name: str = "张三",
    resume: str = CLEAN_RESUME,
    company: str = "杰华特微电子股份有限公司",
    title: str = "技术市场经理",
    education: str = "本科",
    experience: str = "10年",
    source_date: str = "2026-07-20",
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO people(id,display_name,current_company,current_title,city,education,experience)"
            " VALUES (?,?,?,?,'杭州',?,?)",
            (person_id, name, company, title, education, experience),
        )
        conn.execute(
            "INSERT INTO job_candidates(id,job_id,person_id,raw_status,updated_at)"
            " VALUES (?,?,?,'search_shortlisted',datetime('now','localtime'))",
            (candidate_id, job_id, person_id),
        )
        conn.execute(
            "INSERT INTO source_profiles(person_id,source_type,source_candidate_id,source_date,raw_json)"
            " VALUES (?,?,?,?,?)",
            (
                person_id,
                "liepin",
                f"res_{person_id}",
                source_date,
                json.dumps({"full_text": resume, "work_text": resume}, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _fake_llm(risks: dict | None = None, trajectory: dict | None = None) -> FakeLLM:
    return FakeLLM(
        {},
        trajectory=trajectory if trajectory is not None else GOOD_LLM,
        percentile_motivation=GOOD_PM,
        risks=risks,
    )


class DbCase(unittest.TestCase):
    def setUp(self) -> None:
        self.kb_temp = tempfile.TemporaryDirectory()
        kb_dir = Path(self.kb_temp.name) / "kb"
        kb_dir.mkdir(parents=True, exist_ok=True)
        (kb_dir / "kb_company_graph_jsj_v1.json").write_text(
            json.dumps(GRAPH_FIXTURE, ensure_ascii=False), encoding="utf-8"
        )
        self.kb_dir = kb_dir
        self._old_kb_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(kb_dir)
        self.db_temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.db_temp.name) / "asa.db"
        _create_db(self.db_path)

    def tearDown(self) -> None:
        if self._old_kb_env is None:
            os.environ.pop("ASA_KNOWLEDGE_BASE_DIR", None)
        else:
            os.environ["ASA_KNOWLEDGE_BASE_DIR"] = self._old_kb_env
        self.kb_temp.cleanup()
        self.db_temp.cleanup()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def run_assessment(self, llm: FakeLLM, **kwargs) -> dict:
        conn = self.connect()
        try:
            return candidate_assessment.run_assessment(
                conn,
                candidate_id=kwargs.pop("candidate_id", 1),
                job_id=kwargs.pop("job_id", 154),
                llm=llm,
                kb_dir=str(self.kb_dir),
                signal_fetcher=kwargs.pop("signal_fetcher", _stub_fetcher),
                today=TODAY,
                **kwargs,
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 1. 确定性检出：gap / 频繁跳动 / 时间线冲突 / 硬条件差距（纯函数）
# ---------------------------------------------------------------------------

class GapDetectionTest(unittest.TestCase):
    def test_gap_thresholds(self) -> None:
        segments = assessment_signals.parse_work_segments(
            "2021.03-至今 甲公司 · 工程师\n2019.01-2020.06 乙公司 · 工程师", today=TODAY
        )
        items, facts = assessment_signals.detect_resume_gaps(segments)
        assert len(items) == 1, "2020.06→2021.03 = 8 个月空窗 >6 → 检出"
        assert items[0]["severity"] == "medium" and items[0]["kind"] == "gap"
        assert "8 个月" in items[0]["risk"]
        assert len(items[0]["evidence"]) == 2, "空窗证据 = 前后两段所在行"
        assert facts["gaps"][0]["months"] == 8

    def test_gap_over_year_is_high(self) -> None:
        segments = assessment_signals.parse_work_segments(
            "2022.03-至今 甲公司 · 工程师\n2019.01-2020.12 乙公司 · 工程师", today=TODAY
        )
        items, _ = assessment_signals.detect_resume_gaps(segments)
        assert items[0]["severity"] == "high", "14 个月空窗 >12 → high"

    def test_gap_boundary_and_none(self) -> None:
        # 恰好 6 个月 → 不检；相邻衔接 → 不检
        segments = assessment_signals.parse_work_segments(
            "2021.01-至今 甲公司 · 工程师\n2019.01-2020.06 乙公司 · 工程师\n2016.03-2019.01 丙公司 · 工程师",
            today=TODAY,
        )
        items, _ = assessment_signals.detect_resume_gaps(segments)
        assert items == [], "6 个月（不含）以内与相邻衔接都不算空窗"

    def test_degree_lines_not_work_segments(self) -> None:
        # 毕业到第一份工作之间的间隔是正常的，不算空窗
        segments = [
            seg for seg in assessment_signals.parse_work_segments(CLEAN_RESUME, today=TODAY)
            if not assessment_signals._DEGREE_LINE_PATTERN.search(seg["line"])
        ]
        assert len(segments) == 3, "学历段（本科 2009.09-2013.06）不计入工作经历"
        items, _ = assessment_signals.detect_resume_gaps(segments)
        assert items == []


class FrequentHopTest(unittest.TestCase):
    def test_two_short_tenures_medium_three_high(self) -> None:
        segments = assessment_signals.parse_work_segments(
            "2025.01-至今 甲公司 · 工程师\n"
            "2024.01-2024.10 乙公司 · 工程师\n"
            "2022.01-2022.08 丙公司 · 工程师\n"
            "2018.01-2021.12 丁公司 · 工程师",
            today=TODAY,
        )
        items, facts = assessment_signals.detect_frequent_hops(segments, today=TODAY)
        assert len(items) == 1 and items[0]["severity"] == "medium", "2 段短任期 → medium"
        assert len(facts["short_tenure_segments"]) == 2
        segments.append({"start": "2017.01", "end": "2017.09", "is_current": False, "months": 9,
                         "line": "2017.01-2017.09 戊公司 · 工程师"})
        items, _ = assessment_signals.detect_frequent_hops(segments, today=TODAY)
        assert items[0]["severity"] == "high", "3 段短任期 → high"

    def test_recent_window_low_and_stability_bump(self) -> None:
        segments = assessment_signals.parse_work_segments(
            "2025.06-至今 甲公司 · 工程师\n"
            "2023.06-2025.05 乙公司 · 工程师\n"
            "2021.09-2023.05 丙公司 · 工程师",
            today=TODAY,
        )
        items, _ = assessment_signals.detect_frequent_hops(segments, today=TODAY)
        assert len(items) == 1 and items[0]["severity"] == "low", "近 5 年 3 段经历 → low"
        items, _ = assessment_signals.detect_frequent_hops(segments, today=TODAY, stability_sensitive=True)
        assert items[0]["severity"] == "medium", "岗位强调稳定 → 升一档"
        # 短任期 medium + 稳定岗 → high
        segments2 = assessment_signals.parse_work_segments(
            "2025.01-至今 甲公司 · 工程师\n2024.01-2024.10 乙公司 · 工程师\n2022.01-2022.08 丙公司 · 工程师",
            today=TODAY,
        )
        items, _ = assessment_signals.detect_frequent_hops(segments2, today=TODAY, stability_sensitive=True)
        assert items[0]["severity"] == "high"


class TimelineConflictTest(unittest.TestCase):
    def test_overlap_over_six_months(self) -> None:
        segments = assessment_signals.parse_work_segments(
            "2021.01-至今 甲公司 · 工程师\n2020.03-2021.08 乙公司 · 工程师", today=TODAY
        )
        items, facts = assessment_signals.detect_timeline_conflicts(segments)
        assert len(items) == 1, "重叠 8 个月 >6 → 时间线冲突需核实"
        assert items[0]["kind"] == "over_packaging" and items[0]["severity"] == "medium"
        assert facts["timeline_conflicts"][0]["overlap_months"] == 8

    def test_small_overlap_tolerated(self) -> None:
        segments = assessment_signals.parse_work_segments(
            "2021.01-至今 甲公司 · 工程师\n2020.03-2021.03 乙公司 · 工程师", today=TODAY
        )
        items, _ = assessment_signals.detect_timeline_conflicts(segments)
        assert items == [], "3 个月以内重叠按交接期容忍"


class HardGapTest(unittest.TestCase):
    def test_education_and_years_gap_high(self) -> None:
        items, facts = assessment_signals.detect_hard_gaps(
            hard_text="硕士及以上学历，8年以上电源芯片经验",
            education_text="大专",
            experience_text="6年",
            segments=assessment_signals.parse_work_segments(RISKY_RESUME, today=TODAY),
            corpus=RISKY_RESUME,
        )
        kinds = [(item["risk"], item["severity"]) for item in items]
        assert any("学历" in risk and severity == "high" for risk, severity in kinds), "硕士 vs 大专 → high"
        assert any("年限" in risk and severity == "high" for risk, severity in kinds), "8年 vs 6年 → high"
        edu_item = next(item for item in items if "学历" in item["risk"])
        assert edu_item["evidence"], "学历差距证据 = 简历学历行（逐字）"
        assert "大专" in edu_item["evidence"][0]["ref"]

    def test_requirement_met_no_item(self) -> None:
        items, _ = assessment_signals.detect_hard_gaps(
            hard_text="本科及以上学历，5年以上电源芯片经验",
            education_text="本科",
            experience_text="10年",
            segments=[],
            corpus=CLEAN_RESUME,
        )
        assert items == [], "学历年限都达标 → 不出 item"

    def test_skill_missing_low_without_evidence(self) -> None:
        items, facts = assessment_signals.detect_hard_gaps(
            hard_text="5年以上电源芯片经验，熟悉多相控制器",
            education_text="本科",
            experience_text="10年",
            segments=[],
            corpus=CLEAN_RESUME.replace("多相控制器", "XX"),
        )
        skill_items = [item for item in items if "多相控制器" in item["risk"]]
        assert len(skill_items) == 1 and skill_items[0]["severity"] == "low"
        assert skill_items[0]["evidence"] == [], "缺项是 absence 型，本无逐字证据"
        assert facts["skill_terms_missing"] == ["多相控制器"]

    def test_years_floor_not_preferred(self) -> None:
        """"4年以上，优先8年以上"的硬线是 4 年：人选 6 年达标不出 item（取 max 会误报）。"""
        parsed = assessment_signals.parse_hard_requirements("本科及以上；4年以上，优先8年以上；多相控制器")
        assert parsed["years_required"] == 4, "年限硬条件取最低值，『优先 N 年』不是硬线"
        items, _ = assessment_signals.detect_hard_gaps(
            hard_text="本科及以上；4年以上，优先8年以上；多相控制器",
            education_text="本科",
            experience_text="6年",
            segments=[],
            corpus="负责多相控制器产品推广",
        )
        assert items == []

    def test_strategy_pool_companies_not_skill_terms(self) -> None:
        """回归（#564 真实验证暴露）：目标池公司名（MPS/杰华特/晶丰明源）是寻访关键词，
        不是对人要求——不得因"人选不来自友商"报技能缺项。技能词只取硬条件文本。"""
        items, facts = assessment_signals.detect_hard_gaps(
            hard_text="本科及以上；4年以上；多相控制器；DrMOS；POL",
            education_text="硕士",
            experience_text="15年",
            segments=[],
            corpus="负责电源IC应用与市场推广，熟悉多相控制器与POL产品",
        )
        assert facts["skill_terms_missing"] == ["DrMOS"]
        assert all("杰华特" not in item["risk"] and "MPS" not in item["risk"] for item in items)

    def test_no_hard_text_no_check(self) -> None:
        items, _ = assessment_signals.detect_hard_gaps(
            hard_text="", education_text="本科", experience_text="10年",
            segments=[], corpus=CLEAN_RESUME,
        )
        assert items == []

    def test_parse_hard_requirements_tokens(self) -> None:
        parsed = assessment_signals.parse_hard_requirements("硕士及以上学历，8年以上电源芯片经验，熟悉多相控制器，稳定性好")
        assert parsed["edu_required"] == ("硕士", 3)
        assert parsed["years_required"] == 8
        assert parsed["stability_sensitive"] is True
        assert "多相控制器" in parsed["skill_terms"]
        assert all("稳定" not in term for term in parsed["skill_terms"]), "稳定性是态度要求不是技能词"
        assert all("以上" not in term and "学历" not in term for term in parsed["skill_terms"])


# ---------------------------------------------------------------------------
# 2. 主流程集成：五类检出 / LLM 语义项闸 / 空态 / 版本 / markdown
# ---------------------------------------------------------------------------

class RisksIntegrationTest(DbCase):
    def test_deterministic_four_kinds_detected(self) -> None:
        """gap + 频繁跳动 + 硬条件差距（学历/年限/技能缺项）一次跑出；证据逐字。"""
        _seed_person(
            self.db_path, candidate_id=1, job_id=155, person_id=1,
            resume=RISKY_RESUME, company="某半导体公司", title="技术市场总监",
            education="大专", experience="6年",
        )
        doc = self.run_assessment(_fake_llm(), job_id=155)
        risks = doc["dimensions"]["risks"]
        kinds = {item["kind"] for item in risks["items"]}
        assert "gap" in kinds, "8 个月空窗必须检出"
        assert "frequent_hop" in kinds, "两段短任期必须检出"
        assert "hard_requirement" in kinds, "硕士/8年 vs 大专/6年 必须检出"
        risk_texts = [item["risk"] for item in risks["items"]]
        assert any("学历" in text for text in risk_texts) and any("年限" in text for text in risk_texts)
        assert any("多相控制器" in text for text in risk_texts), "技能缺项必须检出"
        for item in risks["items"]:
            assert item["severity"] in {"high", "medium", "low"}
            for evidence in item["evidence"]:
                assert evidence["ref"] in RISKY_RESUME, "每条证据必须逐字存在于简历语料"
        # 有 absence 型条目（技能缺项无证据）→ 维 confidence 必须 inferred
        assert risks["confidence"] == "inferred"
        assert doc["assessor_version"] == "s6-3-v1"
        assert candidate_assessment.validate_assessment(doc) == []

    def test_llm_semantic_items_and_evidence_gate(self) -> None:
        """title 通胀语义项进维度；编造引用整条丢弃；kind 越权丢弃。"""
        _seed_person(
            self.db_path, candidate_id=1, job_id=155, person_id=1,
            resume=RISKY_RESUME, company="某半导体公司", title="技术市场总监",
            education="大专", experience="6年",
        )
        llm = _fake_llm(
            risks={
                "items": [
                    GOOD_RISKS_LLM["items"][0],
                    {"kind": "over_packaging", "risk": "编造的时间线冲突", "severity": "high",
                     "evidence": [{"type": "简历", "ref": "简历里根本没有这句话"}]},
                    {"kind": "gap", "risk": "越权的 gap 语义项", "severity": "low",
                     "evidence": [{"type": "简历", "ref": "2022.05-2022.11 甲科技公司 · 销售工程师 负责客户跟进"}]},
                ]
            }
        )
        doc = self.run_assessment(llm, job_id=155)
        risks = doc["dimensions"]["risks"]
        assert doc["signal_stats"]["risks_llm"] == "ok"
        llm_items = [item for item in risks["items"] if item["kind"] == "title_inflation"]
        assert len(llm_items) == 1, "合法 title 通胀项必须进维度"
        assert llm_items[0]["evidence"][0]["ref"] in RISKY_RESUME
        assert all("编造" not in item["risk"] for item in risks["items"]), "编造证据的 LLM 项必须整条丢弃"
        dropped = risks["stats"]["llm_items_dropped"]
        assert dropped == 2, "编造引用 1 条 + kind 越权 1 条 = 2 条丢弃留痕"
        reasons = [item["reason"] for item in doc["evidence_stats"]["stripped_detail"]]
        assert any("越权" in reason for reason in reasons)
        assert candidate_assessment.validate_assessment(doc) == []

    def test_llm_failure_falls_back_deterministic_only(self) -> None:
        def _broken(payload: dict) -> dict:
            raise candidate_assessment.LLMError("模型超时")

        _seed_person(
            self.db_path, candidate_id=1, job_id=155, person_id=1,
            resume=RISKY_RESUME, company="某半导体公司", title="技术市场总监",
            education="大专", experience="6年",
        )
        llm = FakeLLM({}, trajectory=GOOD_LLM, percentile_motivation=GOOD_PM, risks=_broken)
        doc = self.run_assessment(llm, job_id=155)
        assert doc["signal_stats"]["risks_llm"] == "fallback_deterministic"
        risks = doc["dimensions"]["risks"]
        assert risks["items"], "LLM 挂了确定性项仍在"
        assert all(item["kind"] not in candidate_assessment._RISK_LLM_KINDS for item in risks["items"])
        assert candidate_assessment.validate_assessment(doc) == []

    def test_clean_resume_empty_state(self) -> None:
        """无风险项：items=[] + "未见需核实的问题"，confidence=certain（确实逐项核对过）。"""
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1)
        doc = self.run_assessment(_fake_llm())
        risks = doc["dimensions"]["risks"]
        assert risks["items"] == []
        assert "未见需核实的问题" in risks["verdict"]
        assert risks["confidence"] == "certain"
        assert candidate_assessment.validate_assessment(doc) == []

    def test_severity_enum_validated(self) -> None:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1)
        doc = self.run_assessment(_fake_llm())
        doc["dimensions"]["risks"]["items"] = [
            {"kind": "gap", "risk": "有空窗需核实", "severity": "critical", "evidence": []}
        ]
        errors = candidate_assessment.validate_assessment(doc)
        assert any("severity" in error for error in errors), "severity 非法枚举必须被 validate 拦下"

    def test_markdown_contains_risks_section(self) -> None:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1)
        doc = self.run_assessment(_fake_llm())
        markdown = candidate_assessment._artifact_markdown(doc)
        assert "## 需要核实的问题" in markdown
        assert "未见需核实的问题" in markdown
        assert "不构成任何决策建议" in markdown


# ---------------------------------------------------------------------------
# 3. 敏感扫描复用 + 决策禁语
# ---------------------------------------------------------------------------

class RisksSensitiveScanTest(DbCase):
    def test_llm_risk_text_sensitive_rejected(self) -> None:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1)
        llm = _fake_llm(
            risks={
                "items": [
                    {"kind": "over_packaging", "risk": "35岁还频繁跳槽，需要核实稳定性", "severity": "high",
                     "evidence": [{"type": "简历", "ref": "2021.03-至今 杰华特微电子股份有限公司 · 技术市场经理"}]}
                ]
            }
        )
        conn = self.connect()
        try:
            with self.assertRaises(ValueError) as ctx:
                candidate_assessment.run_assessment(
                    conn, candidate_id=1, job_id=154, llm=llm, kb_dir=str(self.kb_dir),
                    signal_fetcher=_stub_fetcher, today=TODAY,
                )
            assert "敏感" in str(ctx.exception)
            row = conn.execute(
                "SELECT 1 FROM agent_artifacts WHERE artifact_type=?", (candidate_assessment.ARTIFACT_TYPE,)
            ).fetchone()
            assert row is None, "risk 文本命中敏感词必须拒写"
            log = conn.execute(
                "SELECT raw_json FROM candidate_events WHERE event_type='assessment_sensitive_scan_blocked'"
            ).fetchone()
            assert log is not None and "年龄" in str(log["raw_json"])
        finally:
            conn.close()

    def test_llm_risk_text_banned_decision_rejected(self) -> None:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1)
        llm = _fake_llm(
            risks={
                "items": [
                    {"kind": "over_packaging", "risk": "建议淘汰此人", "severity": "high",
                     "evidence": [{"type": "简历", "ref": "2021.03-至今 杰华特微电子股份有限公司 · 技术市场经理"}]}
                ]
            }
        )
        conn = self.connect()
        try:
            with self.assertRaises(ValueError):
                candidate_assessment.run_assessment(
                    conn, candidate_id=1, job_id=154, llm=llm, kb_dir=str(self.kb_dir),
                    signal_fetcher=_stub_fetcher, today=TODAY,
                )
            row = conn.execute(
                "SELECT 1 FROM agent_artifacts WHERE artifact_type=?", (candidate_assessment.ARTIFACT_TYPE,)
            ).fetchone()
            assert row is None, "决策禁语必须拒写（评估只辅助不决策）"
        finally:
            conn.close()

    def test_sensitive_evidence_stripped_item_dropped(self) -> None:
        """LLM 项的简历证据含敏感词 → 证据剥离 → 证据归零的 LLM 项整条丢弃（不拒写整份）。"""
        resume = CLEAN_RESUME + "\n个人情况：41岁 已婚已育 家庭稳定"
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1, resume=resume)
        llm = _fake_llm(
            risks={
                "items": [
                    {"kind": "over_packaging", "risk": "职责描述与时间线存在矛盾，需要核实", "severity": "medium",
                     "evidence": [{"type": "简历", "ref": "个人情况：41岁 已婚已育 家庭稳定"}]}
                ]
            }
        )
        doc = self.run_assessment(llm)
        risks = doc["dimensions"]["risks"]
        assert all("已婚" not in str(item.get("evidence")) for item in risks["items"])
        assert all(item["kind"] != "over_packaging" for item in risks["items"]), "证据归零的 LLM 项必须整条丢弃"
        assert any("敏感" in item["reason"] for item in doc["evidence_stats"]["stripped_detail"])
        assert candidate_assessment.validate_assessment(doc) == []


# ---------------------------------------------------------------------------
# 4. 报告引用块（report_reference_block）
# ---------------------------------------------------------------------------

class ReportReferenceBlockTest(unittest.TestCase):
    def _assessment(self, risks) -> dict:
        return {
            "as_of": "2026-07-24 10:00:00",
            "assessor_version": "s6-3-v1",
            "consultant_summary": "轨迹清晰，跳槽质量高。",
            "dimensions": {
                "trajectory": {"verdict": "一路上行"},
                "percentile": {"band": "top25", "reference": {"n": 10, "years_window": 3, "direction": "技术市场"}},
                "risks": risks,
            },
        }

    def test_block_lines_and_top3_by_severity(self) -> None:
        risks = {
            "items": [
                {"kind": "hard_requirement", "risk": "技能缺项需核实", "severity": "low", "evidence": []},
                {"kind": "gap", "risk": "空窗 8 个月需核实", "severity": "high", "evidence": []},
                {"kind": "frequent_hop", "risk": "两段短任期需核实", "severity": "medium", "evidence": []},
                {"kind": "gap", "risk": "第二条空窗需核实", "severity": "low", "evidence": []},
            ]
        }
        block = candidate_assessment.report_reference_block(self._assessment(risks))
        text = "\n".join(block["lines"])
        assert "一路上行" in text, "trajectory 结论必须进引用块"
        assert "前 25%" in text and "N=10" in text, "分位 band + 参照系必须进引用块"
        assert "轨迹清晰，跳槽质量高。" in text, "顾问口径摘要必须进引用块"
        top = block["top_risks"]
        assert [item["severity"] for item in top] == ["high", "medium", "low"], "top 3 必须按 severity 排序"
        assert "第二条空窗" not in text, "超出 top 3 的不进引用块"
        assert "不构成任何决策建议" in text
        assert block["risks_pending"] is False

    def test_block_empty_risks_honest(self) -> None:
        block = candidate_assessment.report_reference_block(self._assessment({"items": []}))
        assert "未见需核实的问题" in "\n".join(block["lines"])

    def test_block_legacy_assessment_pending(self) -> None:
        block = candidate_assessment.report_reference_block(self._assessment(None))
        assert block["risks_pending"] is True, "旧版评估（risks=null）必须如实标注"
        assert "旧版" in "\n".join(block["lines"])

    def test_block_no_band(self) -> None:
        assessment = self._assessment({"items": []})
        assessment["dimensions"]["percentile"] = {"band": None, "reference": {"n": 0, "years_window": None}}
        block = candidate_assessment.report_reference_block(assessment)
        assert "无法落位" in "\n".join(block["lines"])


if __name__ == "__main__":
    unittest.main()
