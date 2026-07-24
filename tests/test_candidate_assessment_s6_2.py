"""S6-2：判人评估器 —— 水平分位（percentile）+ 动机时机（motivation）测试。

口径：docs/ASA_PRD_S6_判人评估器_2026-07-23.md §2/§5（S6-2 行）。
全部使用临时库 + 临时 KB fixture + FakeLLM + stub 采集器，绝不触碰生产 DB、真实知识库与外网。

覆盖：
1. 分位确定性：构造分布断言 band（top10/top25/median/below）；N < 8 降级 inferred 并注明样本不足；
   模型输出（verdict 措辞/夹带 band 字段）永不可能改 band；LLM 第二调失败降级模板 verdict 但 band 照算；
   参照池过滤（同方向/±3 年/排除本人）；无既有 fit_score 时走轨迹特征 rubric。
2. 动机信号：工况信号计算（在职时长 vs 历史平均任期、近一年简历更新）；公开信号带来源 URL+as_of
   且进 evidence（type=公开信息）；无信号如实"未见明显变动信号"+inferred；编造 URL/知识库引用被拦。
3. 敏感扫描两维复用：percentile/motivation verdict 命中敏感词 → 拒写 + 扫描日志；
   公开信号摘要含敏感词 → 该条信号丢弃（不拒写整份）。
4. 重生成升级：旧 S6-1 artifact（两维 null）经 POST 重生成后两维有值、单行、version 自增。
5. 路由透出：POST/GET 两维结构完整（band/reference/signals）。
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

from fastapi.testclient import TestClient  # noqa: E402

from a_system_agent import assessment_signals, candidate_assessment  # noqa: E402
from a_system_agent.llm import FakeLLM  # noqa: E402
from a_system_agent.schema import ensure_schema  # noqa: E402
from asa_core.app import create_app  # noqa: E402

TODAY = date(2026, 7, 24)

RESUME_TEXT = (
    "张** 求职期望：杭州 技术市场经理\n"
    "2021.03-至今 杰华特微电子股份有限公司 · 技术市场经理 负责PC电源多相控制器产品线市场推广\n"
    "2017.06-2021.02 晶丰明源半导体（上海）股份有限公司 · FAE工程师 负责AC-DC电源芯片客户支持\n"
    "2013.07-2017.05 立讯精密 · 硬件工程师 负责消费电子硬件电路设计\n"
    "浙江大学 · 电子科学与技术 · 本科 2009.09-2013.06"
)

GRAPH_FIXTURE = {
    "companies": {
        "杰华特微电子股份有限公司": {"track": "模拟芯片", "business": "电源管理芯片 多相控制器", "categories": ["设计公司"]},
    }
}

GOOD_LLM = {
    "trajectory": {
        "verdict": "从消费电子硬件转到模拟芯片原厂，技术市场线一路上行",
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
        "verdict": "两次跳槽均为上升",
        "moves": [
            {"from": "晶丰明源", "to": "杰华特", "direction": "up", "platform": "up",
             "title_direction": "up", "responsibility_direction": "up", "reason": "FAE 转技术市场管理"},
        ],
        "current_move": "lateral",
        "evidence": [{"type": "简历", "ref": "2017.06-2021.02 晶丰明源半导体（上海）股份有限公司 · FAE工程师"}],
        "confidence": "certain",
    },
    "consultant_summary": "技术市场线轨迹清晰，两次跳槽均上行。",
}

GOOD_PM = {
    "percentile": {"verdict": "同方向同龄人中处于靠前位置", "evidence": [], "confidence": "certain"},
    "motivation": {"verdict": "在职时长已超其历史平均任期，存在变动的可能", "evidence": [], "confidence": "certain"},
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


def _funding_fetcher(url: str, timeout: float) -> tuple[int, str, str]:
    """公开信号 stub：官网首页给新闻入口，新闻页含一条融资信号。"""
    if "news" in url:
        return 200, "<html><body>公司近日宣布完成新一轮融资，由多家机构联合领投，将用于产线扩建。</body></html>", ""
    return 200, '<html><body><a href="/news/list">新闻中心</a>' + "公司主页介绍。" * 30 + "</body></html>", ""


def _create_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(API_SCHEMA)
        ensure_schema(conn)
        conn.execute("INSERT INTO clients VALUES (1,'士兰微'),(2,'长越科技')")
        conn.execute("INSERT INTO jobs VALUES (154,1,'技术市场经理/总监（PC电源）','PC电源技术市场','5年以上电源芯片经验')")
        conn.execute("INSERT INTO jobs VALUES (137,2,'机械高级工程师','精密机械设计','8年以上精密设备经验')")
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
    resume: str = RESUME_TEXT,
    company: str = "杰华特微电子股份有限公司",
    title: str = "技术市场经理",
    experience: str = "10年",
    source_date: str = "2026-07-20",
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO people(id,display_name,current_company,current_title,city,education,experience)"
            " VALUES (?,?,?,?,'杭州','本科',?)",
            (person_id, name, company, title, experience),
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


def _seed_assessed(
    db_path: Path,
    *,
    candidate_id: int,
    job_id: int,
    person_id: int,
    fit_score: int,
    experience: str = "10年",
    is_current: int = 1,
) -> None:
    """参照池成员：people + job_candidates + agent_candidate_assessments(is_current) 三行。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO people(id,display_name,experience) VALUES (?,?,'')", (person_id, f"参照{person_id}"))
        conn.execute("UPDATE people SET experience=? WHERE id=?", (experience, person_id))
        conn.execute(
            "INSERT OR IGNORE INTO job_candidates(id,job_id,person_id) VALUES (?,?,?)",
            (candidate_id, job_id, person_id),
        )
        conn.execute(
            """
            INSERT INTO agent_candidate_assessments
            (run_id,job_candidate_id,person_id,job_id,snapshot_hash,assessment_version,
             fit_score,fit_level,recommendation,confidence,evidence_coverage,is_current)
            VALUES (?,?,?,?, 'h','v1',?,'B-可推进','hold',0.5,0.5,?)
            """,
            (f"run_{candidate_id}_{is_current}", candidate_id, person_id, job_id, fit_score, is_current),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_reference_pool(db_path: Path, scores: list[int], *, job_id: int = 154, start: int = 100, experience: str = "9年") -> None:
    for index, score in enumerate(scores):
        _seed_assessed(
            db_path,
            candidate_id=start + index,
            job_id=job_id,
            person_id=start + index,
            fit_score=score,
            experience=experience,
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


def _fake_llm(pm: dict | None = None, trajectory: dict | None = None) -> FakeLLM:
    return FakeLLM(
        {},
        trajectory=trajectory if trajectory is not None else GOOD_LLM,
        percentile_motivation=pm if pm is not None else GOOD_PM,
    )


# ---------------------------------------------------------------------------
# 0. 纯函数：方向键 / 年限解析 / 落位 / 轨迹特征分 / 工况解析
# ---------------------------------------------------------------------------

class SignalPureFunctionTest(unittest.TestCase):
    def test_direction_key(self) -> None:
        assert assessment_signals.direction_key("技术市场经理/总监（PC电源）") == "技术市场"
        assert assessment_signals.direction_key("技术市场经理（三次电源/服务器或PC市场）") == "技术市场"
        assert assessment_signals.direction_key("自动化软件高级工程师") == "自动化软件"
        assert assessment_signals.direction_key("机械高级工程师") == "机械"
        assert assessment_signals.direction_key("高级失效分析工程师") == "失效分析"
        assert assessment_signals.direction_key("某种全新稀有岗位") == "某种全新稀有岗位", "无词典命中退化为岗位名"

    def test_parse_experience_years(self) -> None:
        assert assessment_signals.parse_experience_years("14年") == 14
        assert assessment_signals.parse_experience_years("10年以上") == 10
        assert assessment_signals.parse_experience_years("应届") is None

    def test_compute_placement_bands(self) -> None:
        scores = [40, 50, 60, 70, 80, 90, 92, 94, 96, 98]
        assert assessment_signals.compute_placement(99, scores)["band"] == "top10"
        assert assessment_signals.compute_placement(95, scores)["band"] == "top25"  # rank=0.8
        assert assessment_signals.compute_placement(75, scores)["band"] == "median"  # rank=0.4
        assert assessment_signals.compute_placement(30, scores)["band"] == "below"
        # 同分并列：rank=(below+0.5·equal)/N
        tied = assessment_signals.compute_placement(69, [49, 49, 69, 69, 69, 100, 100, 100])
        assert tied["percentile_rank"] == 0.4375 and tied["band"] == "median"
        empty = assessment_signals.compute_placement(80, [])
        assert empty["band"] is None and empty["n"] == 0

    def test_trajectory_feature_score_rubric(self) -> None:
        trajectory = {
            "segments": [{"tier": "T1"}, {"tier": "T2"}],
            "promotion_pace": "fast",
            "tech_evolution": "rising",
        }
        moves = {"moves": [{"direction": "up"}, {"direction": "up"}]}
        score = assessment_signals.trajectory_feature_score(trajectory, moves)
        # base=90*0.5+72*0.5=81；+8(fast)+6(rising)+8(up 均值) = 103 → clamp 100
        assert score == 100
        low = assessment_signals.trajectory_feature_score(
            {"segments": [{"tier": "T3"}], "promotion_pace": "slow", "tech_evolution": "stagnant"},
            {"moves": [{"direction": "down"}]},
        )
        assert low == 48 - 8 - 6 - 8

    def test_parse_work_segments(self) -> None:
        segments = assessment_signals.parse_work_segments(RESUME_TEXT, today=TODAY)
        current = [seg for seg in segments if seg["is_current"]]
        assert len(current) == 1 and current[0]["start"] == "2021.03"
        assert current[0]["months"] == 65  # 2021.03 → 2026.07
        ended = {seg["start"]: seg["months"] for seg in segments if not seg["is_current"]}
        assert ended["2017.06"] == 45 and ended["2013.07"] == 47
        assert all(seg["line"] for seg in segments), "每段必须带所在行原文（逐字证据用）"

    def test_employment_signals_thresholds(self) -> None:
        signals, facts = assessment_signals.employment_signals(
            RESUME_TEXT, latest_source_date="2026-06-22", today=TODAY
        )
        kinds = [sig["kind"] for sig in signals]
        assert "tenure_over_avg" in kinds, "65 个月 vs 历史均值 46 个月 ≥1.2 倍 → 超均值信号"
        assert "resume_recently_updated" in kinds, "32 天前更新 ≤90 天 → 活跃信号"
        assert facts["current_tenure_months"] == 65 and facts["avg_prev_tenure_months"] == 46.0
        # 当前任期远短于历史均值 → under_avg
        short_resume = "2026.05-至今 某新公司 · 工程师\n2017.06-2026.04 某旧公司 · 工程师"
        signals2, _facts2 = assessment_signals.employment_signals(short_resume, today=TODAY)
        assert [sig["kind"] for sig in signals2] == ["tenure_under_avg"]
        # 更新时间在一年外 → 不出更新信号
        signals3, _ = assessment_signals.employment_signals(RESUME_TEXT, latest_source_date="2025-01-01", today=TODAY)
        assert not any(sig["kind"].startswith("resume_") for sig in signals3)

    def test_collect_company_signals_no_hint_and_failure(self) -> None:
        signals, stats = assessment_signals.collect_company_signals("查无此司有限公司", today=TODAY)
        assert signals == [] and "无官网线索" in stats["note"], "无线索必须留痕不静默"
        signals, stats = assessment_signals.collect_company_signals(
            "杰华特微电子股份有限公司", fetcher=_stub_fetcher, today=TODAY
        )
        assert signals == [] and stats["failures"], "抓取失败必须分类留痕"
        assert stats["failures"][0]["category"] == "network_error"

    def test_collect_company_signals_keyword_hit(self) -> None:
        signals, stats = assessment_signals.collect_company_signals(
            "杰华特微电子股份有限公司", fetcher=_funding_fetcher, today=TODAY
        )
        assert len(signals) == 1
        signal = signals[0]
        assert signal["kind"] == "funding" and signal["source"] == "公开信息"
        assert signal["url"].startswith("https://") and signal["as_of"] == "2026-07-24"
        assert stats["pages_fetched"] == 2


# ---------------------------------------------------------------------------
# 1. 分位确定性：构造分布断言 band / N 不足降级 / 模型改不了 band / 池过滤
# ---------------------------------------------------------------------------

class PercentileDeterministicTest(DbCase):
    def test_band_from_constructed_distribution(self) -> None:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1, experience="10年")
        _seed_reference_pool(self.db_path, [40, 50, 60, 70, 80, 90, 92, 94, 96, 98], experience="9年")
        _seed_assessed(self.db_path, candidate_id=1, job_id=154, person_id=1, fit_score=99)
        doc = self.run_assessment(_fake_llm())
        percentile = doc["dimensions"]["percentile"]
        assert percentile["band"] == "top10", "目标 99 分在分布 [40..98] 上必须是 top10"
        assert percentile["basis"] == "fit_score"
        assert percentile["percentile_rank"] == 1.0
        reference = percentile["reference"]
        assert reference["n"] == 10 and reference["direction"] == "技术市场" and reference["years_window"] == 3
        assert reference["sample_sufficient"] is True
        assert percentile["confidence"] == "certain"
        assert candidate_assessment.validate_assessment(doc) == []

    def test_band_median_and_below(self) -> None:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1, experience="10年")
        _seed_reference_pool(self.db_path, [40, 50, 60, 70, 80, 90, 92, 94, 96, 98])
        _seed_assessed(self.db_path, candidate_id=1, job_id=154, person_id=1, fit_score=75)
        doc = self.run_assessment(_fake_llm())
        assert doc["dimensions"]["percentile"]["band"] == "median"  # rank=0.4
        # 换一个低分目标 → below
        conn = self.connect()
        try:
            conn.execute("UPDATE agent_candidate_assessments SET fit_score=30 WHERE job_candidate_id=1")
            conn.commit()
        finally:
            conn.close()
        doc = self.run_assessment(_fake_llm())
        assert doc["dimensions"]["percentile"]["band"] == "below"

    def test_model_cannot_change_band(self) -> None:
        """模型 verdict 声称"前10%"、甚至夹带 band 字段 → 落位只认确定性计算。"""
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1, experience="10年")
        _seed_reference_pool(self.db_path, [80, 82, 84, 86, 88, 90, 92, 94, 96, 98])
        _seed_assessed(self.db_path, candidate_id=1, job_id=154, person_id=1, fit_score=30)
        pm = {
            "percentile": {"verdict": "此人水平前 10%，顶尖", "band": "top10", "evidence": [], "confidence": "certain"},
            "motivation": {"verdict": "未见明显变动信号", "evidence": [], "confidence": "inferred"},
        }
        doc = self.run_assessment(_fake_llm(pm=pm))
        percentile = doc["dimensions"]["percentile"]
        assert percentile["band"] == "below", "band 只能由参照分布算出，模型说了不算"
        assert percentile["percentile_rank"] == 0.0

    def test_small_reference_downgrades_inferred(self) -> None:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1, experience="10年")
        _seed_reference_pool(self.db_path, [60, 70, 80], experience="9年")  # N=3 < 8
        _seed_assessed(self.db_path, candidate_id=1, job_id=154, person_id=1, fit_score=90)
        doc = self.run_assessment(_fake_llm())
        percentile = doc["dimensions"]["percentile"]
        assert percentile["reference"]["n"] == 3
        assert percentile["reference"]["sample_sufficient"] is False
        assert percentile["confidence"] == "inferred", "N<8 必须降级 inferred"
        assert percentile["reference"]["note"], "必须注明样本不足"
        assert "样本不足" in percentile["verdict"] or percentile["verdict"], "verdict 需如实呈现"
        assert candidate_assessment.validate_assessment(doc) == []

    def test_empty_reference_band_null(self) -> None:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1, experience="10年")
        doc = self.run_assessment(_fake_llm())
        percentile = doc["dimensions"]["percentile"]
        assert percentile["band"] is None and percentile["reference"]["n"] == 0
        assert percentile["confidence"] == "inferred"
        assert percentile["basis"] == "trajectory_features", "无既有 fit_score 走轨迹特征 rubric"
        assert candidate_assessment.validate_assessment(doc) == []

    def test_reference_pool_filters(self) -> None:
        """同方向才入池；年限超 ±3 剔除；目标本人剔除；非同岗位同方向可入池。"""
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1, experience="10年")
        # 同方向（技术市场/同岗位）年限 9 年 → 入池 ×8
        _seed_reference_pool(self.db_path, [50, 55, 60, 65, 70, 75, 80, 85], experience="9年")
        # 年限 20 年（|20-10|>3）→ 剔除
        _seed_assessed(self.db_path, candidate_id=200, job_id=154, person_id=200, fit_score=100, experience="20年")
        # 不同方向（机械 job 137）→ 剔除
        _seed_assessed(self.db_path, candidate_id=201, job_id=137, person_id=201, fit_score=100, experience="10年")
        # 年限无法解析 → 剔除（年限过滤开启时）
        _seed_assessed(self.db_path, candidate_id=202, job_id=154, person_id=202, fit_score=100, experience="应届")
        # 目标本人有 fit_score → 不进参照池
        _seed_assessed(self.db_path, candidate_id=1, job_id=154, person_id=1, fit_score=90)
        doc = self.run_assessment(_fake_llm())
        reference = doc["dimensions"]["percentile"]["reference"]
        assert reference["n"] == 8, f"池过滤后应只剩 8 个同方向±3年成员，实际 {reference['n']}"
        assert doc["dimensions"]["percentile"]["band"] == "top10", "90 分在 [50..85] 上为 top10"

    def test_llm_failure_falls_back_template_but_band_computed(self) -> None:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1, experience="10年")
        _seed_reference_pool(self.db_path, [40, 50, 60, 70, 80, 90, 92, 94, 96, 98])
        _seed_assessed(self.db_path, candidate_id=1, job_id=154, person_id=1, fit_score=99)

        def _broken(payload: dict) -> dict:
            raise candidate_assessment.LLMError("模型超时")

        llm = FakeLLM({}, trajectory=GOOD_LLM, percentile_motivation=_broken)
        doc = self.run_assessment(llm)
        percentile = doc["dimensions"]["percentile"]
        assert percentile["band"] == "top10", "LLM 挂了 band 仍由数据算出"
        assert "N=10" in percentile["verdict"] or "前 10%" in percentile["verdict"], "模板 verdict 兜底"
        assert doc["signal_stats"]["pm_llm"] == "fallback_template"
        assert candidate_assessment.validate_assessment(doc) == []


# ---------------------------------------------------------------------------
# 2. 动机信号：工况计算 / 公开信号带来源 / 无信号 inferred / 编造引用被拦
# ---------------------------------------------------------------------------

class MotivationSignalTest(DbCase):
    def test_employment_signals_in_dimension(self) -> None:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1, source_date="2026-06-22")
        doc = self.run_assessment(_fake_llm())
        motivation = doc["dimensions"]["motivation"]
        kinds = [sig["kind"] for sig in motivation["signals"]]
        assert "tenure_over_avg" in kinds and "resume_recently_updated" in kinds
        assert all(sig["source"] == "简历工况" for sig in motivation["signals"])
        refs = [item["ref"] for item in motivation["evidence"]]
        assert any("2021.03-至今" in ref for ref in refs), "工况信号必须挂简历逐字证据行"
        assert motivation["confidence"] == "certain"
        assert candidate_assessment.validate_assessment(doc) == []

    def test_company_public_signal_with_url(self) -> None:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1)
        doc = self.run_assessment(_fake_llm(), signal_fetcher=_funding_fetcher)
        motivation = doc["dimensions"]["motivation"]
        public = [sig for sig in motivation["signals"] if sig["source"] == "公开信息"]
        assert len(public) == 1, "采集到的公开信号必须进 signals"
        signal = public[0]
        assert signal["url"].startswith("https://") and signal["as_of"] == "2026-07-24"
        url_evidence = [item for item in motivation["evidence"] if item["type"] == "公开信息"]
        assert url_evidence and url_evidence[0]["ref"] == signal["url"], "公开信号必须以来源 URL 进证据"
        assert candidate_assessment.validate_assessment(doc) == []

    def test_no_signal_honest_inferred(self) -> None:
        resume = (
            "2023.06-至今 某公司 · 工程师 负责电源设计\n"
            "2020.07-2023.05 另一家公司 · 工程师 负责电源设计\n"
            "2017.07-2020.06 第三家公司 · 工程师 负责硬件设计"
        )
        _seed_person(
            self.db_path, candidate_id=1, job_id=154, person_id=1,
            resume=resume, company="查无此司有限公司", source_date="2025-01-01",
        )
        doc = self.run_assessment(_fake_llm(pm={"percentile": {"verdict": "x", "confidence": "inferred"},
                                                "motivation": {"verdict": "未见明显变动信号，动机需面谈核实",
                                                               "evidence": [], "confidence": "inferred"}}))
        motivation = doc["dimensions"]["motivation"]
        assert motivation["signals"] == [], "任期节奏正常+一年外更新+无公开信号 → 零信号"
        assert motivation["confidence"] == "inferred"
        assert "未见明显变动信号" in motivation["verdict"]
        assert candidate_assessment.validate_assessment(doc) == []

    def test_no_signal_forces_inferred_even_if_model_says_certain(self) -> None:
        resume = "2023.06-至今 某公司 · 工程师 负责电源设计\n2020.07-2023.05 另一家 · 工程师 负责设计"
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1, resume=resume,
                     company="查无此司有限公司", source_date="2025-01-01")
        pm = {"motivation": {"verdict": "他很想动", "evidence": [], "confidence": "certain"}}
        doc = self.run_assessment(_fake_llm(pm=pm))
        assert doc["dimensions"]["motivation"]["confidence"] == "inferred", "无信号时模型说 certain 也必须压回 inferred"

    def test_fabricated_url_and_kb_refs_stripped(self) -> None:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1)
        pm = {
            "percentile": {
                "verdict": "靠前",
                "evidence": [{"type": "知识库", "ref": "历史人选库参照系：同方向（技术市场）±3年 样本N=999 中位分1"},
                             {"type": "公开信息", "ref": "https://fake.example.com/news"}],
                "confidence": "certain",
            },
            "motivation": {
                "verdict": "公司要裁员",
                "evidence": [{"type": "公开信息", "ref": "https://fake.example.com/layoff"}],
                "confidence": "certain",
            },
        }
        doc = self.run_assessment(_fake_llm(pm=pm))
        stripped = doc["evidence_stats"]["stripped_detail"]
        assert any(item["type"] == "知识库" and "非本评估" in item["reason"] for item in stripped), "编造知识库引用必被拦"
        assert sum(1 for item in stripped if item["type"] == "公开信息") == 2, "编造 URL 引用必被拦"
        motivation = doc["dimensions"]["motivation"]
        assert all(item["type"] != "公开信息" for item in motivation["evidence"])


# ---------------------------------------------------------------------------
# 3. 敏感扫描两维复用
# ---------------------------------------------------------------------------

class SensitiveScanS62Test(DbCase):
    def test_percentile_verdict_sensitive_rejected(self) -> None:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1)
        pm = json.loads(json.dumps(GOOD_PM, ensure_ascii=False))
        pm["percentile"]["verdict"] = "35岁正是当打之年，位置靠前"
        conn = self.connect()
        try:
            with self.assertRaises(ValueError) as ctx:
                candidate_assessment.run_assessment(
                    conn, candidate_id=1, job_id=154, llm=_fake_llm(pm=pm), kb_dir=str(self.kb_dir),
                    signal_fetcher=_stub_fetcher, today=TODAY,
                )
            assert "敏感" in str(ctx.exception)
            row = conn.execute(
                "SELECT 1 FROM agent_artifacts WHERE artifact_type=?", (candidate_assessment.ARTIFACT_TYPE,)
            ).fetchone()
            assert row is None, "percentile verdict 命中敏感词必须拒写"
            log = conn.execute(
                "SELECT raw_json FROM candidate_events WHERE event_type='assessment_sensitive_scan_blocked'"
            ).fetchone()
            assert log is not None and "年龄" in str(log["raw_json"])
        finally:
            conn.close()

    def test_motivation_verdict_sensitive_rejected(self) -> None:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1)
        pm = json.loads(json.dumps(GOOD_PM, ensure_ascii=False))
        pm["motivation"]["verdict"] = "已婚已育家庭稳定，动的机会不大"
        conn = self.connect()
        try:
            with self.assertRaises(ValueError):
                candidate_assessment.run_assessment(
                    conn, candidate_id=1, job_id=154, llm=_fake_llm(pm=pm), kb_dir=str(self.kb_dir),
                    signal_fetcher=_stub_fetcher, today=TODAY,
                )
            row = conn.execute(
                "SELECT 1 FROM agent_artifacts WHERE artifact_type=?", (candidate_assessment.ARTIFACT_TYPE,)
            ).fetchone()
            assert row is None, "motivation verdict 命中敏感词必须拒写"
        finally:
            conn.close()

    def test_sensitive_company_signal_dropped_not_rejected(self) -> None:
        """公开页内容不可控：信号摘要含敏感词 → 丢弃该条信号，不拒写整份评估。"""
        def _sensitive_fetcher(url: str, timeout: float) -> tuple[int, str, str]:
            body = "张女士宣布公司完成新一轮融资，业务稳步推进中。" + "公司介绍补充。" * 30
            return 200, f"<html><body>{body}</body></html>", ""

        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1)
        doc = self.run_assessment(_fake_llm(), signal_fetcher=_sensitive_fetcher)
        motivation = doc["dimensions"]["motivation"]
        assert all(sig["source"] != "公开信息" for sig in motivation["signals"]), "含敏感词的公开信号必须丢弃"
        assert doc["signal_stats"]["dropped_sensitive_signals"] == 1
        assert candidate_assessment.validate_assessment(doc) == []


# ---------------------------------------------------------------------------
# 4/5. 重生成升级旧 artifact + 路由两维透出
# ---------------------------------------------------------------------------

class UpgradeAndRouteTest(DbCase):
    def _insert_legacy_s61_artifact(self) -> None:
        """直接插一条 S6-1 形态 artifact（percentile/motivation 为 null 占位）。"""
        legacy = {
            "schema_version": "assessment_v1",
            "candidate_id": 1,
            "job_id": 154,
            "candidate_name_masked": "张**",
            "job_title": "技术市场经理/总监（PC电源）",
            "client": "士兰微",
            "strategy_ref": "",
            "as_of": "2026-07-24 09:00:00",
            "assessor_version": "s6-trajectory-v1",
            "model": "fake-agent-v1",
            "dimensions": {
                "trajectory": {"verdict": "旧结论", "evidence": [{"type": "简历", "ref": "2021.03-至今 杰华特微电子股份有限公司 · 技术市场经理"}],
                               "confidence": "certain", "segments": [], "promotion_pace": "fast", "tech_evolution": "rising"},
                "move_history": {"verdict": "旧结论", "evidence": [{"type": "简历", "ref": "2017.06-2021.02 晶丰明源半导体（上海）股份有限公司 · FAE工程师"}],
                                 "confidence": "certain", "moves": [], "current_move": "lateral"},
                "percentile": None,
                "motivation": None,
                "risks": None,
            },
            "consultant_summary": "旧摘要。",
            "advisor_action": "pending",
            "advisor_note": "",
            "evidence_stats": {"kept": 2, "stripped": 0, "stripped_detail": []},
        }
        conn = self.connect()
        try:
            conn.execute(
                """
                INSERT INTO agent_artifacts
                (artifact_id,goal_id,workflow_id,step_id,artifact_type,title,mime_type,content,metadata_json,validation_status)
                VALUES ('candidate_assessment_1_154','candidate_1','assessment_1_154',NULL,
                        'candidate_assessment','判人评估：张** × 技术市场经理/总监（PC电源） v1','text/markdown','# 旧',?,'passed')
                """,
                (json.dumps(legacy, ensure_ascii=False),),
            )
            conn.commit()
        finally:
            conn.close()

    def test_regenerate_upgrades_legacy_artifact(self) -> None:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1)
        _seed_reference_pool(self.db_path, [40, 50, 60, 70, 80, 90, 92, 94, 96, 98])
        _seed_assessed(self.db_path, candidate_id=1, job_id=154, person_id=1, fit_score=99)
        self._insert_legacy_s61_artifact()
        app = create_app(db_path=self.db_path, start_legacy=False)
        app.state.core.agent_service.llm = _fake_llm()
        app.state.core.agent_service.assessment_signal_fetcher = _funding_fetcher
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/candidates/1/assessments?job_id=154",
                json={"request_id": "req-u1"}, headers={"Idempotency-Key": "k-u1"},
            )
            assert response.status_code == 200, response.text
            doc = response.json()["assessment"]
            percentile = doc["dimensions"]["percentile"]
            motivation = doc["dimensions"]["motivation"]
            assert percentile and percentile["band"] == "top10", "旧 artifact 重生成后分位必须有值"
            assert motivation["signals"], "旧 artifact 重生成后动机信号必须有值"
            assert isinstance(doc["dimensions"]["risks"], dict), "旧 artifact 重生成后 risks 必须填充（S6-3）"
            assert doc["assessor_version"] == "s6-3-v1"
            assert doc["version"] == 2, "重生成走幂等更新，version 自增"
            # GET 透出两维
            fetched = client.get("/api/v1/candidates/1/assessments?job_id=154")
            assert fetched.status_code == 200
            got = fetched.json()["assessment"]
            assert got["dimensions"]["percentile"]["band"] == "top10"
            assert got["dimensions"]["percentile"]["reference"]["n"] == 10
            public = [sig for sig in got["dimensions"]["motivation"]["signals"] if sig["source"] == "公开信息"]
            assert public and public[0]["url"].startswith("https://") and public[0]["as_of"]
        # 单行不重复建
        conn = self.connect()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM agent_artifacts WHERE artifact_type=?", (candidate_assessment.ARTIFACT_TYPE,)
            ).fetchone()[0]
            assert count == 1
        finally:
            conn.close()

    def test_route_exposes_two_dimensions(self) -> None:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1)
        app = create_app(db_path=self.db_path, start_legacy=False)
        app.state.core.agent_service.llm = _fake_llm()
        app.state.core.agent_service.assessment_signal_fetcher = _stub_fetcher
        with TestClient(app) as client:
            created = client.post(
                "/api/v1/candidates/1/assessments?job_id=154",
                json={"request_id": "req-r1"}, headers={"Idempotency-Key": "k-r1"},
            )
            assert created.status_code == 200, created.text
            doc = created.json()["assessment"]
            assert set(doc["dimensions"].keys()) == {"trajectory", "move_history", "percentile", "motivation", "risks"}
            risks = doc["dimensions"]["risks"]
            assert isinstance(risks, dict) and isinstance(risks["items"], list), "S6-3 起 risks 必须填充"
            percentile = doc["dimensions"]["percentile"]
            assert percentile["band"] is None and percentile["confidence"] == "inferred", "空参照池如实无法落位"
            motivation = doc["dimensions"]["motivation"]
            assert motivation["signals"], "工况信号应存在"
            fetched = client.get("/api/v1/candidates/1/assessments?job_id=154")
            assert fetched.json()["assessment"]["dimensions"]["motivation"]["signals"] == motivation["signals"]


# ---------------------------------------------------------------------------
# 6. 回测工具：band × 实际推进对照表 + 错例归因 + 导出（临时库 + FakeLLM）
# ---------------------------------------------------------------------------

class BacktestToolTest(DbCase):
    def _seed_outcome_person(self, index: int, *, stage: str, fit_score: int) -> None:
        resume = (
            f"人** 求职期望：杭州 技术市场经理（人选{index}）\n"
            "2020.05-至今 杰华特微电子股份有限公司 · 技术市场经理 负责PC电源产品线\n"
            "2015.04-2020.04 晶丰明源半导体（上海）股份有限公司 · FAE工程师 负责客户支持\n"
            "2011.07-2015.03 立讯精密 · 硬件工程师 负责硬件设计\n"
            "杭州电子科技大学 · 电子信息工程 · 本科 2007.09-2011.06"
        )
        _seed_person(
            self.db_path, candidate_id=index, job_id=154, person_id=index,
            name=f"候选人{index}", resume=resume, experience="10年",
        )
        conn = self.connect()
        try:
            conn.execute("UPDATE job_candidates SET clean_stage=? WHERE id=?", (stage, index))
            conn.execute(
                """
                INSERT INTO agent_candidate_assessments
                (run_id,job_candidate_id,person_id,job_id,snapshot_hash,assessment_version,
                 fit_score,fit_level,recommendation,confidence,evidence_coverage,is_current)
                VALUES (?,?,?,154,'h','v1',?,'B-可推进','hold',0.5,0.5,1)
                """,
                (f"run_bt_{index}", index, index, fit_score),
            )
            conn.commit()
        finally:
            conn.close()

    def test_backtest_table_and_attribution(self) -> None:
        import assessment_percentile_backtest

        # 6 推进（高分）+ 6 初筛未过（低分）：分位与实际结果应一致
        for index in range(1, 7):
            self._seed_outcome_person(index, stage="已触达", fit_score=88 + index)
        for index in range(7, 13):
            self._seed_outcome_person(index, stage="H5 最近寻访/初筛不通过", fit_score=20 + index)
        out_dir = Path(self.db_temp.name) / "work" / "assessment_replay"
        summary = assessment_percentile_backtest.run_backtest(
            self.db_path, limit=12, out_dir=out_dir, llm=_fake_llm(),
            kb_dir=str(self.kb_dir), signal_fetcher=_stub_fetcher, today=TODAY,
        )
        assert summary["generated"] == 12 and summary["attempted"] == 12, summary
        table = {row["band"]: row for row in summary["table"]}
        assert table["top10"]["advanced_rate"] == 1.0, "高分位应全是推进组"
        assert table["below"]["advanced_rate"] == 0.0, "低分位应全是初筛未过组"
        # 推进组内部分差（89/90/91）落在 median：严格口径下计 3 例错例，归因必须非空
        assert summary["mismatch_count"] == 3
        assert all(item["attribution"] for item in summary["mismatches"])
        assert all(item["group"] == "advanced" and item["band"] == "median" for item in summary["mismatches"])
        markdown = Path(summary["markdown"]).read_text(encoding="utf-8")
        assert "band × 实际推进率" in markdown and "错例" in markdown
        assert str(out_dir) in summary["markdown"], "回测导出只进 work/assessment_replay/"
        assert Path(summary["json"]).is_file()

    def test_backtest_mismatch_attribution_rules(self) -> None:
        import assessment_percentile_backtest

        # 构造错例：推进组但 fit 极低（落 below）
        for index in range(1, 10):
            self._seed_outcome_person(index, stage="已触达" if index == 1 else "H5 最近寻访/初筛不通过",
                                      fit_score=10 if index == 1 else 60 + index)
        out_dir = Path(self.db_temp.name) / "work" / "assessment_replay"
        summary = assessment_percentile_backtest.run_backtest(
            self.db_path, limit=9, out_dir=out_dir, llm=_fake_llm(),
            kb_dir=str(self.kb_dir), signal_fetcher=_stub_fetcher, today=TODAY,
        )
        assert summary["mismatch_count"] >= 1, "推进组落 below 必须进错例"
        mismatch = summary["mismatches"][0]
        assert mismatch["group"] == "advanced" and mismatch["band"] == "below"
        assert mismatch["attribution"], "错例必须有一句归因"
        assert "参照" in mismatch["attribution"] or "简历" in mismatch["attribution"] or "偏差" in mismatch["attribution"]


if __name__ == "__main__":
    unittest.main()
