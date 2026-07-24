"""S6-4 端到端真实验收（验收①，正式库少量写入，任务卡允许）：

找一条 assessment（candidate_assessment_569_154，士兰微 × 技术市场经理/总监（PC电源））
→ PATCH modified 写改判口径 → 校准集出现该样例 → 同人同岗重新生成
→ 校验新评估 payload 注入了改判样例、结论对比前后差异（脱敏记录）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from a_system_agent import assessment_calibration, candidate_assessment  # noqa: E402
from a_system_agent.llm import create_default_llm  # noqa: E402
from a_system_agent.workflow import _mask_candidate_name  # noqa: E402
from asa_core.database import DEFAULT_DB  # noqa: E402

CANDIDATE_ID, JOB_ID = 569, 154
NOTE = "分位判断口径偏保守：这个客户做PC电源技术市场，接受平移，不卡分位，更看产品线匹配度"


class CaptureLLM:
    """包装真实 LLM：捕获三次调用的 payload（验证校准注入），其余透传。"""

    def __init__(self, inner):
        self._inner = inner
        self.model = getattr(inner, "model", "unknown")
        self.captured: dict = {}

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def assess_trajectory(self, payload):
        self.captured["trajectory"] = payload
        return self._inner.assess_trajectory(payload)

    def assess_percentile_motivation(self, payload):
        self.captured["pm"] = payload
        return self._inner.assess_percentile_motivation(payload)

    def assess_risks(self, payload):
        self.captured["risks"] = payload
        return self._inner.assess_risks(payload)


def main() -> None:
    conn = sqlite3.connect(str(DEFAULT_DB), timeout=30)
    conn.row_factory = sqlite3.Row
    before = candidate_assessment.get_assessment(conn, CANDIDATE_ID, JOB_ID)
    assert before, "目标评估不存在"
    bdoc = before["assessment"]
    print("== 改判前 ==")
    print("advisor_action:", bdoc.get("advisor_action"))
    print("姓名遮罩:", bdoc.get("candidate_name_masked"), "｜客户:", bdoc.get("client"), "｜岗位:", bdoc.get("job_title"))
    print("轨迹结论:", (bdoc["dimensions"]["trajectory"] or {}).get("verdict"))
    print("分位结论:", (bdoc["dimensions"]["percentile"] or {}).get("verdict"))
    print("口径摘要:", bdoc.get("consultant_summary"))

    # 1) 顾问改判写回（modified + 口径 note）
    result = candidate_assessment.apply_advisor_action(
        conn, candidate_id=CANDIDATE_ID, job_id=JOB_ID, action="modified", note=NOTE
    )
    conn.commit()
    print("\n== 改判写回 ==")
    print("calibration 回执:", json.dumps(result["calibration"], ensure_ascii=False))

    # 2) 校准集出现该样例
    row = conn.execute(
        f"SELECT * FROM {assessment_calibration.TABLE} WHERE artifact_id=?",
        (f"candidate_assessment_{CANDIDATE_ID}_{JOB_ID}",),
    ).fetchone()
    assert row, "校准集未出现样例"
    print("样例已入库: id=", row["id"], "｜维度:", row["dimensions_json"], "｜客户:", row["client"], "｜岗位类型:", row["job_type"])

    # 3) 同人同岗重新生成（真实 LLM，捕获 payload 验证注入）
    llm = create_default_llm()
    if getattr(llm, "model", "unavailable") == "unavailable":
        print("\n!! 真实 LLM 不可用（无 key），中止重新生成步骤")
        return
    capture = CaptureLLM(llm)
    new_doc = candidate_assessment.run_assessment(
        conn, candidate_id=CANDIDATE_ID, job_id=JOB_ID, llm=capture, mask_name=_mask_candidate_name
    )
    artifact_id = candidate_assessment.persist_assessment(conn, new_doc)
    conn.commit()
    block = capture.captured.get("trajectory", {}).get("calibration")
    print("\n== 重新生成（校准注入） ==")
    print("注入样例数:", new_doc["calibration_stats"]["samples_injected"])
    assert block and block["examples"], "校准段未注入"
    print("注入样例口径:", block["examples"][0]["advisor_correction"])
    assert "calibration" in capture.captured.get("pm", {}) and "calibration" in capture.captured.get("risks", {})
    print("三次 LLM 调用均携带校准段 ✓")

    print("\n== 前后对比（脱敏） ==")
    print("[前] 分位结论:", (bdoc["dimensions"]["percentile"] or {}).get("verdict"))
    print("[后] 分位结论:", (new_doc["dimensions"]["percentile"] or {}).get("verdict"))
    print("[前] 轨迹结论:", (bdoc["dimensions"]["trajectory"] or {}).get("verdict"))
    print("[后] 轨迹结论:", (new_doc["dimensions"]["trajectory"] or {}).get("verdict"))
    print("[前] 口径摘要:", bdoc.get("consultant_summary"))
    print("[后] 口径摘要:", new_doc.get("consultant_summary"))
    blob = json.dumps(new_doc, ensure_ascii=False)
    assert NOTE not in blob, "改判 note 不得落 artifact"
    print("改判 note 未落入 artifact ✓；artifact:", artifact_id, "version:", new_doc.get("version"))

    # 4) 度量一致性
    metrics = assessment_calibration.compute_metrics(conn)
    print("\n== 度量 ==")
    print("totals:", metrics["totals"])
    conn.close()


if __name__ == "__main__":
    main()
