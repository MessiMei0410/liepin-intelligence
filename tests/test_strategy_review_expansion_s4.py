"""S4-3c-3（N3）：池枯竭信号 + 扩池决策树测试。

口径：docs/ASA_寻访链路完整优化方案_2026-07-23.md N3 + ASA_KIMI_TASK_S4-3c_S4-5_2026-07-23.md S4-3c-3。
全部使用临时库 + 临时 KB fixture（运行时只读，绝不触碰真实知识库与生产 DB）。
覆盖：dedupe_rate 81%/79%/80% 边界与阈值可配置、信号与 verdict 正交（healthy/quality_gap +
饱和各一例）、决策树 5 步结构与 params 真实来源（禁止编造）、无策略时降级留空 + notes、
artifact 幂等（rebuild 覆盖 + history）、GET /strategy-review 透出、restricted 不回泄。
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
    STRATEGY_V2_FIXTURE,
    ReviewDbCase,
    _funnel_row,
    _write_review_kb,
)

# N3 岗位原型 fixture（两种形态共享子结构）：
# - EXPANSION_SEED_FIXTURE：写临时 KB 的原始 seed 形态（load_job_archetypes 解析 job_archetype 包裹层）
# - EXPANSION_ARCHETYPE：load_job_archetypes 归一化后的形态（纯函数 build_strategy_review 直传）
# 用于验证决策树 params 全部取真实值（T2 池名/当前词组/地点策略可逐项对账）。
_POOL_FIXTURE = {
    "T2_customer_OEM": {
        "companies": [{"name": "下游X公司"}, {"name": "下游Y公司"}],
        "rationale": "逆向客户整机厂",
    },
    "T3_adjacent_unconfirmed": {
        "companies": [{"name": "相邻Z公司"}],
        "rationale": "相邻池（顾问已确认启用）",
    },
}
_GROUPS_FIXTURE = [
    {"group": "kb_broad", "targets": "放宽技术词", "terms": ["真空腔体", "传动机构"]},
    {"group": "kb_func", "targets": "职能词", "terms": ["系统工程师"]},
]

EXPANSION_SEED_FIXTURE = {
    "job_archetype": {
        "archetype_id": "a1", "title": "精密设备机械", "client": "长越科技",
        "essence": "精密设备机械核心岗", "directions": [], "target_functions": [],
        "location_policy": "杭州优先，上海/苏州次之",
    },
    "target_company_pool": _POOL_FIXTURE,
    "keyword_groups": _GROUPS_FIXTURE,
    "negative_rules": [],
    "level_mapping": {},
}

EXPANSION_ARCHETYPE = {
    "archetype_id": "a1", "title": "精密设备机械", "client": "长越科技",
    "essence": "精密设备机械核心岗", "directions": [], "target_functions": [],
    "location_policy": "杭州优先，上海/苏州次之",
    "level_mapping": {},
    "keyword_groups": _GROUPS_FIXTURE,
    "negative_rules": [],
    "target_company_pool": _POOL_FIXTURE,
    "source_file": "seed_a1_mech_v1.json",
}

_FIXTURE_T2_NAMES = {"下游X公司", "下游Y公司"}
_FIXTURE_TERMS = {"真空腔体", "传动机构", "系统工程师"}

_DEFAULT_STRATEGY = object()


def _row(
    channel: str = "liepin",
    status: str = "completed",
    recall: int = 120,
    extracted: int = 100,
    dedupe: int = 81,
    unique: int = 19,
    intake_new: int = 19,
    assessed: int = 19,
    high: int = 5,
    zero_attribution: str | None = None,
) -> dict:
    """漏斗行：_funnel_row 不支持 extracted/dedupe，覆盖为 N3 口径（81% 排重缺省）。"""
    row = _funnel_row(
        channel, status=status, recall=recall, unique=unique, intake_new=intake_new,
        assessed=assessed, high=high, detail=(unique, 0, 0), zero_attribution=zero_attribution,
    )
    row["extracted_count"] = extracted
    row["dedupe_count"] = dedupe
    return row


def _build(rows: list[dict], strategy: object = _DEFAULT_STRATEGY, **kwargs) -> dict:
    kwargs.setdefault("archetype", EXPANSION_ARCHETYPE)
    return strategy_review.build_strategy_review(
        workflow_id="wf-n3",
        strategy_doc=STRATEGY_V2_FIXTURE if strategy is _DEFAULT_STRATEGY else strategy,
        funnel_rows=rows,
        **kwargs,
    )


def _steps_by_action(review: dict) -> dict[str, dict]:
    return {step["action_type"]: step for step in review["expansion_decision_tree"]}


class PoolSaturatedSignalTest(unittest.TestCase):
    """轮次级 dedupe_rate 信号：81%/79%/80% 边界、阈值可配置、extracted=0 不计。"""

    def test_81_percent_triggers_signal(self) -> None:
        review = _build([_row(extracted=100, dedupe=81)])
        assert review["evidence"]["dedupe_rate"] == 0.81
        assert review["evidence"]["extracted_total"] == 100
        assert review["evidence"]["dedupe_total"] == 81
        assert len(review["signals"]) == 1
        signal = review["signals"][0]
        assert signal["signal"] == "pool_saturated"
        assert signal["scope"] == "round"
        assert signal["dedupe_rate"] == 0.81
        assert signal["threshold"] == 0.8
        assert signal["channels"][0]["channel"] == "liepin"
        # 口径分层留痕：轮次信号（>80%）与渠道级 0 归因（>90%）语义不同层
        assert "zero_attribution" in signal["semantics"]
        # 信号写入 verdict 决策依据（业务语言），且声明与结论互不影响
        assert "重复率" in review["verdict_reason"]
        assert "互不影响" in review["verdict_reason"]
        assert review["thresholds"]["pool_saturated_dedupe_rate"] == 0.8

    def test_79_percent_does_not_trigger(self) -> None:
        review = _build([_row(extracted=100, dedupe=79)])
        assert review["evidence"]["dedupe_rate"] == 0.79
        assert review["signals"] == []
        assert review["expansion_decision_tree"] == []
        assert "pool_saturated" not in review["verdict_reason"]

    def test_exactly_80_percent_does_not_trigger(self) -> None:
        # 口径为严格大于（>80%），恰好 80% 不触发
        review = _build([_row(extracted=100, dedupe=80)])
        assert review["evidence"]["dedupe_rate"] == 0.8
        assert review["signals"] == []
        assert review["expansion_decision_tree"] == []

    def test_threshold_configurable(self) -> None:
        review = _build([_row(extracted=100, dedupe=81)], dedupe_rate_threshold=0.85)
        assert review["signals"] == [], "阈值可配置：0.81 ≤ 0.85 不触发"
        assert review["thresholds"]["pool_saturated_dedupe_rate"] == 0.85
        review_low = _build([_row(extracted=100, dedupe=79)], dedupe_rate_threshold=0.75)
        assert len(review_low["signals"]) == 1, "0.79 > 0.75 触发"

    def test_extracted_zero_not_counted(self) -> None:
        review = _build([_row(extracted=0, dedupe=0, recall=0, unique=0, intake_new=0, assessed=0, high=0)])
        assert review["evidence"]["dedupe_rate"] is None, "extracted=0 不计轮次排重率"
        assert review["signals"] == []

    def test_aggregates_across_channels(self) -> None:
        # 轮次级口径：跨渠道聚合 Σdedupe/Σextracted（49+33=82，60+40=100 → 82%）
        rows = [
            _row("liepin", extracted=60, dedupe=49, unique=11, intake_new=11, assessed=11, high=4),
            _row("xsaas", extracted=40, dedupe=33, unique=7, intake_new=7, assessed=7, high=2),
        ]
        review = _build(rows)
        assert review["evidence"]["dedupe_rate"] == 0.82
        assert len(review["signals"]) == 1
        assert {c["channel"] for c in review["signals"][0]["channels"]} == {"liepin", "xsaas"}


class ExpansionDecisionTreeTest(unittest.TestCase):
    """扩池决策树：5 步有序结构 + params 只取 strategy_v2/漏斗/原型真实值（禁止编造）。"""

    def test_five_steps_ordered_with_real_params(self) -> None:
        review = _build([_row()])
        tree = review["expansion_decision_tree"]
        assert len(tree) == 5
        assert [step["order"] for step in tree] == [1, 2, 3, 4, 5], "order 递增"
        assert [step["step_id"] for step in tree] == [f"exp-{i}" for i in range(1, 6)]
        assert [step["action_type"] for step in tree] == list(strategy_review.EXPANSION_ACTION_TYPES)
        for step in tree:
            assert step["status"] == "pending", "每步可逐项采纳/拒绝（pending→accepted/rejected）"
            assert step["title"].strip() and step["detail"].strip()

        steps = _steps_by_action(review)

        # 1 换关键词组：当前组取 strategy_v2.step4，候选组取原型 keyword_groups（逐字对账）
        swap = steps["swap_keywords"]["params"]
        assert swap["current_groups"] == [
            {"group": "core", "targets": "T1 友商", "terms": ["精密机械", "运动台"]}
        ]
        assert {g["group"] for g in swap["candidate_groups"]} == {"kb_broad", "kb_func"}
        candidate_terms = {term for g in swap["candidate_groups"] for term in g["terms"]}
        assert candidate_terms <= _FIXTURE_TERMS, "候选词必须来自原型，禁止编造"

        # 2 扩池：当前层 T1（取 strategy_v2.step2），下一层 T2 公司取原型池（禁止编造）
        expand = steps["expand_pool"]["params"]
        assert expand["current_tiers"] == ["T1"]
        assert expand["next_tier"] == "T2"
        assert expand["tier_label"] == "T2 客户整机厂"
        assert expand["companies"] == ["下游X公司", "下游Y公司"]
        assert set(expand["companies"]) <= _FIXTURE_T2_NAMES, "扩池公司必须来自原型 T2 池"
        assert expand["rationale"] == "逆向客户整机厂"
        assert "a1" in expand["source_archetype"]

        # 3 放宽条件：职级取 step3、地点取原型 location_policy；年限无来源留空 + note
        relax = steps["relax_condition"]["params"]
        items = {item["field"]: item for item in relax["items"]}
        assert list(items) == ["年限", "职级", "地点"]
        assert items["年限"]["current"] is None and items["年限"]["note"].strip()
        assert items["职级"]["current"] == ["高级工程师", "经理"]
        assert items["职级"]["proposal"].strip() and items["职级"]["cost"].strip()
        assert items["地点"]["current"] == "杭州优先，上海/苏州次之"
        assert items["地点"]["cost"].strip()
        assert "restricted" in relax["boundary"], "放宽边界：不触碰禁挖/竞业等 restricted 约束"

        # 4 渠道再平衡：引用本轮漏斗转化率（19/19 = 100% → 倾斜猎聘）
        rebalance = steps["rebalance_channel"]["params"]
        assert rebalance["channel_stats"][0]["channel"] == "liepin"
        assert rebalance["channel_stats"][0]["intake_conversion"] == 1.0
        assert rebalance["recommended_channel"] == "liepin"

        # 5 升级项：转 Mapping 直挖 / 与客户校准方向
        escalate = steps["escalate_mapping"]
        assert escalate["order"] == 5
        assert escalate["params"]["actions"] == ["mapping_direct_sourcing", "client_direction_calibration"]

    def test_tree_without_strategy_degrades_with_notes(self) -> None:
        # 无 strategy_v2：树仍强制产出（信号正交），取不到的 params 留空 + notes 说明
        review = _build([_row()], strategy=None)
        assert review["verdict"] == "insufficient_data"
        assert len(review["signals"]) == 1
        tree = review["expansion_decision_tree"]
        assert len(tree) == 5
        steps = _steps_by_action(review)
        assert steps["swap_keywords"]["params"]["current_groups"] == []
        # 原型候选组仍在（KB 可用），扩池下一层为 T1 但原型无 T1 块 → 留空
        assert steps["swap_keywords"]["params"]["candidate_groups"], "原型候选组不依赖 strategy_v2"
        assert steps["expand_pool"]["params"]["companies"] == []
        assert steps["expand_pool"]["params"]["current_tiers"] == []
        # 渠道再平衡不依赖策略对象，仍给出真实转化
        assert steps["rebalance_channel"]["params"]["recommended_channel"] == "liepin"
        assert any("留空" in note or "待顾问" in note for note in review["notes"]), "留空必须有 notes 说明"


class VerdictSignalOrthogonalTest(unittest.TestCase):
    """信号与四判定分支正交：healthy+饱和、quality_gap+饱和各一例（verdict 不变，树强制产出）。"""

    def test_healthy_with_saturation(self) -> None:
        # 召回达标（120 ≥ 40×50%）、高分率 5/19=26% ≥ 15% → healthy；排重 81% → 信号+树
        review = _build([_row()])
        assert review["verdict"] == "healthy"
        assert len(review["signals"]) == 1
        assert len(review["expansion_decision_tree"]) == 5
        assert review["revision_diff"] == [], "healthy 无修订 diff，但决策树照常产出"

    def test_quality_gap_with_saturation(self) -> None:
        # 高分率 1/15=6.7% < 15% → quality_gap；排重 85% → 信号+树；diff 与树并存
        review = _build([_row(extracted=100, dedupe=85, unique=15, intake_new=15, assessed=15, high=1)])
        assert review["verdict"] == "quality_gap"
        assert len(review["signals"]) == 1
        assert len(review["expansion_decision_tree"]) == 5
        assert review["revision_diff"], "quality_gap 的 step1 复核 diff 与决策树并存"
        assert review["revision_diff"][0]["step"] == "step1_job_essence"
        assert review["escalation"]["kind"] == "evaluation_issue_ticket", "既有 escalation 语义不回归"


def _insert_funnel(
    db_path: Path,
    workflow_id: str,
    run_id: str,
    *,
    channel: str = "liepin",
    recall: int = 120,
    extracted: int = 100,
    dedupe: int = 81,
    unique: int = 19,
    intake_new: int = 19,
    assessed: int = 19,
    high: int = 5,
) -> None:
    conn = sqlite3.connect(db_path)
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
                run_id, workflow_id, 10, "长越科技", "机械高级工程师", channel, "completed", 1, "[]",
                recall, extracted, dedupe, unique, unique, 0, 0, 0, intake_new, assessed, high, None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


class ExpansionDbCase(ReviewDbCase):
    """临时库 + 临时 KB（覆盖默认原型为 N3 fixture：含地点策略/T2/T3 池/候选关键词组）。"""

    def setUp(self) -> None:
        super().setUp()
        _write_review_kb(Path(self.kb_temp.name) / "kb", archetype=EXPANSION_SEED_FIXTURE)


class ExpansionTreePersistenceTest(ExpansionDbCase):
    """决策树写回复盘 artifact：rebuild 幂等覆盖 + history 保留 + GET 透出 + restricted 不回泄。"""

    def test_tree_upsert_idempotent_and_get_exposes(self) -> None:
        self.make_terminal_workflow("wf-sat", created_at="2026-07-22 10:00:00")
        self.insert_strategy_artifact("wf-sat")
        _insert_funnel(self.db_path, "wf-sat", "run-1")

        first = self.service.rebuild_strategy_review("wf-sat")
        review = first["review"]
        assert review["version"] == 1
        assert review["verdict"] == "healthy", "召回达标+高分率 26% → healthy（信号正交）"
        assert len(review["signals"]) == 1
        tree = review["expansion_decision_tree"]
        assert len(tree) == 5
        steps = {step["action_type"]: step for step in tree}
        # 真实 KB 装配路径：T2 公司与候选词组来自临时 KB 原型（非编造，可对账 fixture）
        assert steps["expand_pool"]["params"]["companies"] == ["下游X公司", "下游Y公司"]
        assert "seed_a1_mech_v1.json" in steps["expand_pool"]["params"]["source_archetype"]
        assert {g["group"] for g in steps["swap_keywords"]["params"]["candidate_groups"]} == {"kb_broad", "kb_func"}

        # 重算（加一行 xsaas 漏斗）：树随 upsert 覆盖重算，version 自增，history 保留 v1
        _insert_funnel(
            self.db_path, "wf-sat", "run-2",
            channel="xsaas", recall=40, extracted=40, dedupe=33, unique=7, intake_new=7, assessed=7, high=3,
        )
        second = self.service.rebuild_strategy_review("wf-sat")
        assert second["artifact_id"] == first["artifact_id"]
        assert second["review"]["version"] == 2
        assert second["review"]["evidence"]["dedupe_rate"] == round(114 / 140, 4)
        assert len(second["review"]["expansion_decision_tree"]) == 5, "rebuild 重算覆盖，树仍产出"
        history = second["review"]["history"]
        assert len(history) == 1 and history[0]["version"] == 1 and history[0]["verdict"] == "healthy"

        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM agent_artifacts WHERE workflow_id=? AND artifact_type='strategy_review'",
                ("wf-sat",),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1, "幂等 upsert：同一工作流只保留一条 strategy_review"

        # GET（service 层透出）：signals / tree / evidence / thresholds 齐全；markdown content 渲染决策树
        loaded = self.service.get_strategy_review("wf-sat")
        assert loaded["review"]["signals"][0]["signal"] == "pool_saturated"
        assert len(loaded["review"]["expansion_decision_tree"]) == 5
        assert loaded["review"]["thresholds"]["pool_saturated_dedupe_rate"] == 0.8
        assert "人不够时的扩圈建议" in loaded["content"]
        assert "swap_keywords" in loaded["content"]

        # restricted 边界：策略对象含禁挖名单字面量，复盘输出（含决策树）绝不回泄
        encoded = json.dumps(loaded, ensure_ascii=False)
        for literal in FORBIDDEN_LITERALS + ["青岛芯恩", "福建晋华"]:
            assert literal not in encoded, f"restricted 字面量不得出现在复盘输出（含决策树）：{literal}"


class ExpansionApiTest(unittest.TestCase):
    """API：GET /strategy-review 透出 signals 与 expansion_decision_tree。

    schema 口径同 test_strategy_review_s4.StrategyReviewApiTest（migrate 需要 candidates.source 等列）。
    """

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
        kb_dir = _write_review_kb(Path(self.kb_temp.name) / "kb", archetype=EXPANSION_SEED_FIXTURE)
        self._old_kb_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(kb_dir)

    def tearDown(self) -> None:
        if self._old_kb_env is None:
            os.environ.pop("ASA_KNOWLEDGE_BASE_DIR", None)
        else:
            os.environ["ASA_KNOWLEDGE_BASE_DIR"] = self._old_kb_env
        self.kb_temp.cleanup()

    def _create_db(self, db_path: Path) -> None:
        conn = sqlite3.connect(db_path)
        conn.executescript(self.API_SCHEMA)
        conn.execute("INSERT INTO clients VALUES (1,'长越科技')")
        conn.execute("INSERT INTO jobs VALUES (10,1,'机械高级工程师')")
        conn.commit()
        conn.close()

    def _seed_saturated(self, db_path: Path, workflow_id: str) -> None:
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
                "run-api-sat", workflow_id, 10, "长越科技", "机械高级工程师", "liepin", "completed", 1, "[]",
                120, 100, 81, 19, 19, 0, 0, 19, 19, 5,
            ),
        )
        conn.commit()
        conn.close()

    def test_get_api_exposes_signal_and_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "asa.db"
            self._create_db(db_path)
            app = create_app(db_path=db_path, start_legacy=False)
            self._seed_saturated(db_path, "wf-api-sat")
            with TestClient(app) as client:
                posted = client.post(
                    "/api/v1/workflows/wf-api-sat/strategy-review/rebuild",
                    json={"request_id": "req-n3-1"}, headers={"Idempotency-Key": "n3-key-1"},
                )
                assert posted.status_code == 200, posted.text
                assert posted.json()["review"]["expansion_decision_tree"], "rebuild 响应即含决策树"

                got = client.get("/api/v1/workflows/wf-api-sat/strategy-review")
                assert got.status_code == 200
                review = got.json()["review"]
                assert review["signals"][0]["signal"] == "pool_saturated"
                assert review["evidence"]["dedupe_rate"] == 0.81
                tree = review["expansion_decision_tree"]
                assert [step["action_type"] for step in tree] == list(strategy_review.EXPANSION_ACTION_TYPES)
                assert [step["order"] for step in tree] == [1, 2, 3, 4, 5]
                for step in tree:
                    assert step["status"] == "pending"
                steps = {step["action_type"]: step for step in tree}
                assert steps["expand_pool"]["params"]["companies"] == ["下游X公司", "下游Y公司"]
                assert "seed_a1_mech_v1.json" in steps["expand_pool"]["params"]["source_archetype"]
                assert steps["relax_condition"]["params"]["items"][2]["current"] == "杭州优先，上海/苏州次之"


if __name__ == "__main__":
    unittest.main()
