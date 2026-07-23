"""S4-5（N5）：评估校准暴露测试。

口径：docs/ASA_寻访链路完整优化方案_2026-07-23.md N5 + ASA_KIMI_TASK_S4-3c_S4-5_2026-07-23.md S4-5。
全部使用临时库 + 临时 KB fixture（运行时只读，绝不触碰真实知识库与生产 DB）。
覆盖：
- 触发边界：高分率 0 且评估数 ≥5 触发；4 评估不触发；有高分不触发；
  触发但取不到证据链时条目仍附（items=[] + notes 说明）；
- 证据链真实：遮罩名/当前公司职位/fit_score/扣分证据（criteria_json 的 not_met 准则 +
  gaps_json 缺口，硬伤在前，≤3 条），按 fit_score 从高到低取 ≤3 个被否人选；
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
    ReviewDbCase,
    _build,
    _funnel_row,
)

EVAL_ITEMS_FIXTURE = [
    {
        "job_candidate_id": 301, "assessment_id": 401, "candidate": "李**",
        "company": "下游X公司", "title": "机械工程师", "fit_score": 72,
        "fit_level": "B-可推进", "recommendation": "verify_first",
        "deductions": [
            {"group": "hard_requirements", "criterion": "7年以上精密设备机械设计经验", "status": "not_met",
             "critical": True, "reason": "年限不足", "evidence": ["仅4年相关经验"]}
        ],
    },
    {
        "job_candidate_id": 302, "assessment_id": 402, "candidate": "韩**",
        "company": "ASM中国集团公司", "title": "设备工程师", "fit_score": 60,
        "fit_level": "C-需确认", "recommendation": "hold",
        "deductions": [],
    },
]


class EvaluationReviewTriggerTest(unittest.TestCase):
    """纯函数触发边界：0 高分 × ≥5 评估触发；4 评估不触发；有高分不触发。"""

    def test_zero_high_with_five_assessed_attaches_review(self) -> None:
        rows = [_funnel_row("liepin", recall=40, unique=20, intake_new=20, assessed=5, high=0, detail=(20, 0, 0))]
        review = _build(rows, evaluation_items=EVAL_ITEMS_FIXTURE)
        assert review["verdict"] == "quality_gap", "高分率 0 仍按既有分支判 quality_gap（不回归）"
        entry = review["evaluation_review"]
        assert entry is not None, "0 高分 × 5 评估必须附评估尺度复核条目"
        assert entry["prompt"] == "是尺严还是人不行"
        assert entry["assessed_total"] == 5 and entry["high_score_total"] == 0
        assert len(entry["items"]) == 2
        assert entry["items"][0]["candidate"] == "李**", "证据链摘要原样并入（装配层已遮罩）"
        assert "评估尺度复核" in review["verdict_reason"]
        assert review["thresholds"]["evaluation_review_min_assessed"] == 5

    def test_four_assessed_does_not_trigger(self) -> None:
        rows = [_funnel_row("liepin", recall=40, unique=20, intake_new=20, assessed=4, high=0, detail=(20, 0, 0))]
        review = _build(rows, evaluation_items=EVAL_ITEMS_FIXTURE)
        assert review["evaluation_review"] is None, "评估数 4 < 5 不触发"
        assert "评估尺度复核" not in review["verdict_reason"]

    def test_any_high_score_does_not_trigger(self) -> None:
        rows = [_funnel_row("liepin", recall=40, unique=20, intake_new=20, assessed=6, high=1, detail=(20, 0, 0))]
        review = _build(rows, evaluation_items=EVAL_ITEMS_FIXTURE)
        assert review["evaluation_review"] is None, "有高分（≥1）不触发"

    def test_triggers_with_empty_items_and_note(self) -> None:
        rows = [_funnel_row("liepin", recall=40, unique=20, intake_new=20, assessed=5, high=0, detail=(20, 0, 0))]
        review = _build(rows, evaluation_items=[])
        entry = review["evaluation_review"]
        assert entry is not None and entry["items"] == [], "触发但取不到证据链时条目仍附（items 为空）"
        assert any("未取到被否人选证据链" in note for note in review["notes"])

    def test_min_assessed_threshold_configurable(self) -> None:
        rows = [_funnel_row("liepin", recall=40, unique=20, intake_new=20, assessed=3, high=0, detail=(20, 0, 0))]
        review = _build(rows, evaluation_items=EVAL_ITEMS_FIXTURE, evaluation_review_min_assessed=3)
        assert review["evaluation_review"] is not None, "阈值可配置：3 ≥ 3 触发"
        assert review["thresholds"]["evaluation_review_min_assessed"] == 3


def _criteria_json() -> str:
    return json.dumps(
        {
            "hard_requirements": [
                {"criterion": "7年以上精密设备机械设计经验", "status": "not_met", "critical": True,
                 "evidence": ["仅4年相关经验"], "reason": "年限不足"},
            ],
            "core_abilities": [
                {"criterion": "有限元", "status": "not_met", "critical": False,
                 "evidence": ["履历未提及有限元"], "reason": "技能缺失"},
                {"criterion": "运动台", "status": "met", "critical": False,
                 "evidence": ["负责运动台装配"], "reason": ""},
            ],
            "soft_preferences": [],
        },
        ensure_ascii=False,
    )


def _seed_assessments(db_path: Path, specs: list[tuple[int, int, str, int, str, str]]) -> None:
    """specs: (jc_id, person_id, display_name, fit_score, company, title)，全部 is_current=1、run 完成。"""
    conn = sqlite3.connect(db_path)
    try:
        for index, (jc_id, person_id, name, fit_score, company, title) in enumerate(specs, 1):
            conn.execute(
                "INSERT INTO people (id,display_name,current_company,current_title) VALUES (?,?,?,?)",
                (person_id, name, company, title),
            )
            conn.execute(
                "INSERT INTO job_candidates (id,job_id,person_id,raw_client,raw_position,clean_stage) VALUES (?,?,?,?,?,?)",
                (jc_id, 10, person_id, "长越科技", "机械高级工程师", "S1 新增寻访/待复核"),
            )
            run_id = f"run-assess-{index}"
            conn.execute(
                """
                INSERT INTO agent_runs (run_id,kind,context_type,context_id,snapshot_hash,status)
                VALUES (?,?,?,?,?,?)
                """,
                (run_id, "candidate_assessment", "candidate", jc_id, f"snap-{index}", "completed"),
            )
            conn.execute(
                """
                INSERT INTO agent_candidate_assessments
                (run_id,job_candidate_id,candidate_id,person_id,job_id,client,job,snapshot_hash,
                 assessment_version,fit_score,fit_level,recommendation,confidence,evidence_coverage,
                 criteria_json,strengths_json,gaps_json,is_current)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id, jc_id, None, person_id, 10, "长越科技", "机械高级工程师", f"snap-{index}",
                    "assessment_v1", fit_score, "C-需确认" if fit_score < 70 else "B-可推进",
                    "not_recommended" if fit_score < 55 else "verify_first", 0.8, 0.9,
                    _criteria_json(), '["态度积极"]', '["缺乏整机厂客户经验"]', 1,
                ),
            )
        conn.commit()
    finally:
        conn.close()


class EvaluationReviewDbTest(ReviewDbCase):
    """DB 装配路径：证据链取 agent_candidate_assessments 真实字段，遮罩名，≤3 条，按分高者先取。"""

    SPECS = [
        (301, 201, "李雷", 72, "下游X公司", "机械工程师"),
        (302, 202, "韩梅梅", 68, "ASM中国集团公司", "设备工程师"),
        (303, 203, "王建国", 60, "下游Y公司", "工艺工程师"),
        (304, 204, "赵小燕", 55, "相邻Z公司", "机械设计师"),
        (305, 205, "陈志强", 40, "下游X公司", "助理工程师"),
    ]

    def _rebuild_with_funnel(self, *, assessed: int, high: int) -> dict:
        self.make_terminal_workflow("wf-n5", created_at="2026-07-22 10:00:00")
        self.insert_strategy_artifact("wf-n5")
        self.insert_funnel("wf-n5", "run-n5", channel="liepin", recall=40, unique=20, intake_new=20,
                           assessed=assessed, high=high, detail=(20, 0, 0))
        return self.service.rebuild_strategy_review("wf-n5")["review"]

    def test_five_zero_high_attaches_real_evidence_chain(self) -> None:
        _seed_assessments(self.db_path, self.SPECS)
        review = self._rebuild_with_funnel(assessed=5, high=0)
        entry = review["evaluation_review"]
        assert entry is not None
        assert entry["prompt"] == "是尺严还是人不行"
        items = entry["items"]
        assert len(items) == 3, "≤3 个被否人选"
        # 按 fit_score 从高到低：72/68/60（最接近高分线者最能分辨尺严/人不行）
        assert [item["fit_score"] for item in items] == [72, 68, 60]
        top = items[0]
        assert top["candidate"] == "李**", "候选人姓名必须遮罩"
        assert "李雷" not in json.dumps(entry, ensure_ascii=False), "明文姓名不得出现在证据链"
        assert top["company"] == "下游X公司" and top["title"] == "机械工程师", "当前公司职位取真实字段"
        assert top["job_candidate_id"] == 301 and top["assessment_id"], "回链候选人详情的锚点齐全"
        # 关键扣分证据：硬伤在前（hard_requirements not_met），核心能力缺口次之，≤3 条
        deductions = top["deductions"]
        assert deductions[0]["group"] == "hard_requirements" and deductions[0]["critical"] is True
        assert deductions[0]["criterion"] == "7年以上精密设备机械设计经验"
        assert deductions[0]["evidence"] == ["仅4年相关经验"]
        assert deductions[1]["criterion"] == "有限元", "not_met 准则逐条取真实字段"
        assert all(ded["status"] == "not_met" for ded in deductions), "只取扣分项，met 不混入"
        assert any(ded["group"] == "gaps" for ded in deductions), "gaps_json 缺口并入证据链"
        # markdown content 渲染尺度复核区
        loaded = self.service.get_strategy_review("wf-n5")
        assert "评估尺度复核" in loaded["content"] and "李**" in loaded["content"]

    def test_four_assessed_not_triggered_in_db(self) -> None:
        _seed_assessments(self.db_path, self.SPECS[:4])
        review = self._rebuild_with_funnel(assessed=4, high=0)
        assert review["evaluation_review"] is None

    def test_triggered_without_assessment_rows_degrades_to_empty_items(self) -> None:
        review = self._rebuild_with_funnel(assessed=5, high=0)
        entry = review["evaluation_review"]
        assert entry is not None and entry["items"] == []
        assert any("未取到被否人选证据链" in note for note in review["notes"])

    def test_review_output_never_echoes_restricted(self) -> None:
        _seed_assessments(self.db_path, self.SPECS)
        self._rebuild_with_funnel(assessed=5, high=0)
        payload = self.service.get_strategy_review("wf-n5")
        assert payload["review"]["evaluation_review"] is not None, "前置：尺度复核已触发"
        encoded = json.dumps(payload, ensure_ascii=False)
        for literal in FORBIDDEN_LITERALS + ["青岛芯恩", "福建晋华"]:
            assert literal not in encoded, f"restricted 字面量不得出现在复盘输出（含评估尺度复核）：{literal}"


if __name__ == "__main__":
    unittest.main()
