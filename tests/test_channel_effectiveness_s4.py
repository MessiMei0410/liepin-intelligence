"""S4-5（N4）：渠道效能学习测试。

口径：docs/ASA_寻访链路完整优化方案_2026-07-23.md N4 + ASA_KIMI_TASK_S4-3c_S4-5_2026-07-23.md S4-5。
全部使用临时库 + 临时 KB fixture（运行时只读，绝不触碰真实知识库与生产 DB）。
覆盖：
- 累积口径：渠道×岗位原型逐轮累积 rounds/recall_total/intake_total/high_score_total/conversion；
  无 archetype 归 'unknown' 桶；同一工作流重算（rebuild 幂等）不重复累积；
- zero_streak：连续 0 召回（非渠道故障归因）逐轮 +1，有召回清零；
  session_expired/page_structure_changed/loading_incomplete 故障轮不累加也不打断；
- 降权：zero_streak≥2 产出 channel_downweights 留痕（复盘 + strategy_v2 写回 +
  扩池决策树 rebalance_channel 步 params），连续两轮后降权建议出现、召回恢复后消失；
- restricted 字面量不回泄。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from a_system_agent import strategy_review  # noqa: E402
from test_strategy_review_s4 import (  # noqa: E402
    FORBIDDEN_LITERALS,
    STRATEGY_V2_FIXTURE,
    ReviewDbCase,
    _build,
    _funnel_row,
)


def _effect_row(db_path: Path, channel: str, archetype_id: str = "a1") -> dict | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM agent_channel_effectiveness WHERE channel=? AND archetype_id=?",
            (channel, archetype_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _strategy_metadata(db_path: Path, workflow_id: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT metadata_json FROM agent_artifacts WHERE workflow_id=? AND artifact_type='search_strategy'",
            (workflow_id,),
        ).fetchone()
        return json.loads(row[0]) if row else {}
    finally:
        conn.close()


class EffectivenessAccumulationTest(ReviewDbCase):
    """累积口径：渠道×原型逐轮累积；'unknown' 桶；rebuild 幂等不重复累积。"""

    def _make_round(self, workflow_id: str, created_at: str, *, with_strategy: bool = True) -> None:
        self.make_terminal_workflow(workflow_id, created_at=created_at)
        if with_strategy:
            self.insert_strategy_artifact(workflow_id)

    def test_accumulates_rounds_and_totals_per_channel_archetype(self) -> None:
        self._make_round("wf-e1", "2026-07-20 10:00:00")
        self._make_round("wf-e2", "2026-07-21 10:00:00")
        self.insert_funnel("wf-e1", "run-e1", channel="liepin", recall=100, unique=20, intake_new=10, assessed=8, high=2, detail=(20, 0, 0))
        self.insert_funnel("wf-e2", "run-e2", channel="liepin", recall=50, unique=10, intake_new=5, assessed=4, high=1, detail=(10, 0, 0))

        self.service.rebuild_strategy_review("wf-e1")
        row = _effect_row(self.db_path, "liepin")
        assert row is not None, "复盘生成时必须落 agent_channel_effectiveness 行"
        assert (row["rounds"], row["recall_total"], row["intake_total"], row["high_score_total"]) == (1, 100, 10, 2)
        assert row["conversion"] == 0.1, "conversion = intake_total/recall_total"
        assert row["archetype_id"] == "a1", "archetype 取 strategy_v2.archetype_id"
        assert row["zero_streak"] == 0
        assert row["last_verdict"], "last_verdict 记当轮判定"

        self.service.rebuild_strategy_review("wf-e2")
        row = _effect_row(self.db_path, "liepin")
        assert (row["rounds"], row["recall_total"], row["intake_total"], row["high_score_total"]) == (2, 150, 15, 3), (
            "同 渠道×原型 逐轮累积"
        )
        assert row["conversion"] == 0.1

    def test_missing_archetype_falls_into_unknown_bucket(self) -> None:
        self._make_round("wf-e3", "2026-07-20 10:00:00", with_strategy=False)
        self.insert_funnel("wf-e3", "run-e3", channel="xsaas", recall=30, unique=6, intake_new=6, assessed=6, high=3, detail=(6, 0, 0))
        self.service.rebuild_strategy_review("wf-e3")
        row = _effect_row(self.db_path, "xsaas", archetype_id="unknown")
        assert row is not None, "无 archetype 归 'unknown' 桶"
        assert row["rounds"] == 1 and row["recall_total"] == 30
        assert _effect_row(self.db_path, "xsaas", archetype_id="a1") is None

    def test_rebuild_same_workflow_does_not_double_count(self) -> None:
        self._make_round("wf-e4", "2026-07-20 10:00:00")
        self.insert_funnel("wf-e4", "run-e4", channel="liepin", recall=0, zero_attribution="no_results")
        first = self.service.rebuild_strategy_review("wf-e4")
        second = self.service.rebuild_strategy_review("wf-e4")
        assert second["review"]["version"] == 2, "rebuild 照常覆盖复盘（version 自增）"
        row = _effect_row(self.db_path, "liepin")
        assert row["rounds"] == 1, "同一工作流重算不重复累积 rounds"
        assert row["zero_streak"] == 1, "同一工作流重算不重复累积 zero_streak"
        assert row["last_verdict"] == second["review"]["verdict"], "重算仅刷新判定留痕"
        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM agent_channel_effectiveness").fetchone()[0]
        finally:
            conn.close()
        assert count == 1, "UNIQUE(channel, archetype_id) 约束下不重复建行"


class ZeroStreakTest(ReviewDbCase):
    """zero_streak：连续 0 召回（非渠道故障）+1；有召回清零；渠道故障轮不累加也不打断。"""

    def _round(self, workflow_id: str, created_at: str, **funnel) -> dict:
        self.make_terminal_workflow(workflow_id, created_at=created_at)
        self.insert_strategy_artifact(workflow_id)
        self.insert_funnel(workflow_id, f"run-{workflow_id}", channel="xsaas", **funnel)
        return self.service.rebuild_strategy_review(workflow_id)["review"]

    def test_streak_increments_on_non_fault_zero_recall(self) -> None:
        self._round("wf-z1", "2026-07-20 10:00:00", recall=0, zero_attribution="no_results")
        assert _effect_row(self.db_path, "xsaas")["zero_streak"] == 1
        self._round("wf-z2", "2026-07-21 10:00:00", recall=0, zero_attribution=None)
        row = _effect_row(self.db_path, "xsaas")
        assert row["zero_streak"] == 2, "归因缺失也算非渠道故障 0 召回"
        assert row["rounds"] == 2

    def test_recall_resets_streak(self) -> None:
        self._round("wf-z3", "2026-07-20 10:00:00", recall=0, zero_attribution="no_results")
        self._round("wf-z4", "2026-07-21 10:00:00", recall=0, zero_attribution="query_build_error")
        assert _effect_row(self.db_path, "xsaas")["zero_streak"] == 2
        review = self._round("wf-z5", "2026-07-22 10:00:00", recall=25, unique=5, intake_new=5, assessed=5, high=2, detail=(5, 0, 0))
        row = _effect_row(self.db_path, "xsaas")
        assert row["zero_streak"] == 0, "有召回清零"
        assert review["channel_downweights"] == [], "清零后降权建议消失"

    def test_channel_fault_rounds_neither_increment_nor_reset(self) -> None:
        self._round("wf-z6", "2026-07-20 10:00:00", recall=0, zero_attribution="no_results")
        for index, attribution in enumerate(("session_expired", "page_structure_changed", "loading_incomplete"), 1):
            self._round(f"wf-z7-{index}", f"2026-07-2{index} 11:00:00", recall=0, status="blocked", zero_attribution=attribution)
            row = _effect_row(self.db_path, "xsaas")
            assert row["zero_streak"] == 1, f"渠道故障归因 {attribution} 不累加 streak"
        # 故障轮不打断：再来一轮非故障 0 召回，streak 从 1 → 2（而非从 0 重计）
        self._round("wf-z8", "2026-07-25 10:00:00", recall=0, zero_attribution="no_results")
        assert _effect_row(self.db_path, "xsaas")["zero_streak"] == 2, "渠道故障轮不打断连续计数"


class DownweightTraceTest(ReviewDbCase):
    """降权留痕：连续两轮 0 召回 → 复盘 channel_downweights + strategy_v2 写回 + 决策树再平衡步标注。"""

    def _insert_liepin_saturated(self, workflow_id: str, run_id: str) -> None:
        """猎聘行：召回 120、排重 85/100（>80% 触发池枯竭决策树）、入库 15、高分 3。"""
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
                    run_id, workflow_id, 10, "长越科技", "机械高级工程师", "liepin", "completed", 1, "[]",
                    120, 100, 85, 15, 15, 0, 0, 0, 15, 15, 3, None,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _zero_round(self, workflow_id: str, created_at: str) -> dict:
        self.make_terminal_workflow(workflow_id, created_at=created_at)
        self.insert_strategy_artifact(workflow_id)
        self.insert_funnel(workflow_id, f"run-{workflow_id}", channel="xsaas", recall=0, zero_attribution="no_results")
        self._insert_liepin_saturated(workflow_id, f"run-{workflow_id}-lp")
        return self.service.rebuild_strategy_review(workflow_id)["review"]

    def test_second_consecutive_zero_round_produces_downweight(self) -> None:
        first = self._zero_round("wf-d1", "2026-07-20 10:00:00")
        assert first["channel_downweights"] == [], "首轮 0 召回（streak=1）不降权"

        second = self._zero_round("wf-d2", "2026-07-21 10:00:00")
        downweights = second["channel_downweights"]
        assert len(downweights) == 1, "连续两轮 0 召回（streak≥2）产出降权建议"
        entry = downweights[0]
        assert entry["channel"] == "xsaas" and entry["archetype_id"] == "a1"
        assert entry["streak"] == 2 and entry["rounds"] == 2
        assert "连续 2 轮 0 召回" in entry["reason"]
        assert "非渠道故障" in entry["reason"]
        assert "降权" in entry["recommendation"] and "顾问确认" in entry["recommendation"], (
            "本期仅建议不执行：配额调整待顾问确认"
        )
        assert "渠道效能学习" in second["verdict_reason"], "降权写入判定依据"
        # 高效渠道（liepin 有召回）不降权
        assert all(item["channel"] != "liepin" for item in downweights)
        # 复盘暴露累积快照（X-SaaS × a1 累计数据可见）
        snapshot = next(item for item in second["channel_effectiveness"] if item["channel"] == "xsaas")
        assert snapshot["zero_streak"] == 2 and snapshot["rounds"] == 2

    def test_downweight_traced_into_strategy_v2_and_tree(self) -> None:
        self._zero_round("wf-d3", "2026-07-20 10:00:00")
        review = self._zero_round("wf-d4", "2026-07-21 10:00:00")
        assert review["channel_downweights"], "前置：连续两轮 0 召回已降权"

        # strategy_v2 留痕（同一写动作写回策略对象）
        strategy_doc = _strategy_metadata(self.db_path, "wf-d4").get("strategy_v2") or {}
        traced = strategy_doc.get("channel_downweights") or []
        assert len(traced) == 1 and traced[0]["channel"] == "xsaas" and traced[0]["streak"] == 2
        assert any("channel_downweights" in note for note in review["notes"])

        # 扩池决策树（排重 85%>80% 触发）rebalance_channel 步带降权 params
        tree = review["expansion_decision_tree"]
        assert len(tree) == 5, "排重率 85% 触发池枯竭信号与决策树"
        rebalance = next(step for step in tree if step["action_type"] == "rebalance_channel")
        assert rebalance["params"]["downweights"][0]["channel"] == "xsaas"
        assert "降权" in rebalance["detail"], "再平衡步 detail 标注降权建议"
        assert rebalance["params"]["recommended_channel"] == "liepin", "配额倾斜仍有入库转化的高效渠道"

    def test_downweight_clears_after_recall_recovers(self) -> None:
        self._zero_round("wf-d5", "2026-07-20 10:00:00")
        self._zero_round("wf-d6", "2026-07-21 10:00:00")
        assert _effect_row(self.db_path, "xsaas")["zero_streak"] == 2
        self.make_terminal_workflow("wf-d7", created_at="2026-07-22 10:00:00")
        self.insert_strategy_artifact("wf-d7")
        self.insert_funnel("wf-d7", "run-d7", channel="xsaas", recall=30, unique=6, intake_new=6, assessed=6, high=1, detail=(6, 0, 0))
        review = self.service.rebuild_strategy_review("wf-d7")["review"]
        assert review["channel_downweights"] == [], "召回恢复后不再降权"
        strategy_doc = _strategy_metadata(self.db_path, "wf-d7").get("strategy_v2") or {}
        assert strategy_doc.get("channel_downweights") == [], "策略对象留痕同步清空（覆写为最新清单）"

    def test_review_output_never_echoes_restricted(self) -> None:
        self._zero_round("wf-d8", "2026-07-20 10:00:00")
        review = self._zero_round("wf-d9", "2026-07-21 10:00:00")
        assert review["channel_downweights"], "前置：已产出降权"
        payload = self.service.get_strategy_review("wf-d9")
        encoded = json.dumps(payload, ensure_ascii=False)
        for literal in FORBIDDEN_LITERALS + ["青岛芯恩", "福建晋华"]:
            assert literal not in encoded, f"restricted 字面量不得出现在复盘输出（含降权留痕）：{literal}"


class DownweightPureFunctionTest(unittest.TestCase):
    """纯函数：compute_channel_downweights 阈值；build_strategy_review 并入降权（不触碰 DB）。"""

    def test_compute_threshold(self) -> None:
        rows = [
            {"channel": "xsaas", "archetype_id": "a1", "zero_streak": 2, "rounds": 3, "recall_total": 10},
            {"channel": "liepin", "archetype_id": "a1", "zero_streak": 1, "rounds": 3, "recall_total": 300},
        ]
        downweights = strategy_review.compute_channel_downweights(rows)
        assert [item["channel"] for item in downweights] == ["xsaas"]
        assert strategy_review.compute_channel_downweights(rows, min_streak=3) == [], "阈值可配置"

    def test_build_review_attaches_downweights_and_annotates_tree(self) -> None:
        downweights = strategy_review.compute_channel_downweights(
            [{"channel": "xsaas", "archetype_id": "a1", "zero_streak": 2, "rounds": 2, "recall_total": 0}]
        )
        rows = [
            _funnel_row("liepin", recall=120, unique=15, intake_new=15, assessed=15, high=3, detail=(15, 0, 0)),
            _funnel_row("xsaas", recall=0, zero_attribution="no_results"),
        ]
        for row in rows:
            row["extracted_count"] = 100 if row["channel"] == "liepin" else 0
            row["dedupe_count"] = 85 if row["channel"] == "liepin" else 0
        review = _build(rows, channel_downweights=downweights)
        assert review["channel_downweights"] == downweights
        assert "渠道效能学习" in review["verdict_reason"]
        rebalance = next(step for step in review["expansion_decision_tree"] if step["action_type"] == "rebalance_channel")
        assert rebalance["params"]["downweights"][0]["streak"] == 2
        assert review["thresholds"]["zero_streak_downweight"] == 2

    def test_build_review_without_downweights_keeps_empty_list(self) -> None:
        review = _build([_funnel_row("liepin", recall=35, unique=20, intake_new=20, assessed=10, high=5, detail=(20, 0, 0))])
        assert review["channel_downweights"] == []
        assert "渠道效能学习" not in review["verdict_reason"]


if __name__ == "__main__":
    unittest.main()
