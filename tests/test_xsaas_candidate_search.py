from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from xsaas_candidate_search import (
    DETAIL_JS,
    DETAIL_READY_JS,
    apply_position_score_gate,
    capture_candidate_details,
    query_matches,
)


class XsaasQueryRoundBindingTest(unittest.TestCase):
    """任务卡 UX-1 问题 B：每轮关键词与结果集做轮次绑定，防止串词错配。"""

    def test_binding_accepts_exact_and_contained_selection(self) -> None:
        self.assertTrue(query_matches("MPS 工程师", "MPS 工程师"))
        self.assertTrue(query_matches("MPS", "MPS 工程师 深圳"))
        self.assertTrue(query_matches("  MPS   工程师 ", "MPS 工程师"))

    def test_binding_rejects_stale_or_empty_selection(self) -> None:
        self.assertFalse(query_matches("MPS", ""))
        self.assertFalse(query_matches("MPS", "DrMOS 驱动"))
        self.assertFalse(query_matches("", "MPS"))


class XsaasCandidateDetailCaptureTest(unittest.TestCase):
    def test_detail_extractor_contains_structured_resume_sections(self) -> None:
        self.assertIn("工作经历", DETAIL_JS)
        self.assertIn("项目经历", DETAIL_JS)
        self.assertIn("教育经历", DETAIL_JS)
        self.assertIn("full_text", DETAIL_JS)

    def test_detail_waits_for_matching_loaded_candidate(self) -> None:
        self.assertIn("oCurrentCandidate", DETAIL_READY_JS)
        self.assertIn("bVitaeloading", DETAIL_READY_JS)
        self.assertIn("candidateId === String(expectedId)", DETAIL_READY_JS)

    def test_detail_capture_replaces_list_summary_and_marks_complete(self) -> None:
        candidate = {
            "xsaas_id": "5566778",
            "profile_text": "列表页摘要",
            "source_url": "https://headhunt.x-saas.com.cn/#/app/candidate/info/5566778",
        }
        detail = {
            "xsaas_id": "5566778",
            "source_url": candidate["source_url"],
            "full_text": "X-SaaS完整履历 " * 20,
            "work_text": "某精密设备有限公司\n运动控制软件工程师\n2020.01-至今\n负责EtherCAT控制",
            "project_text": "晶圆传输项目\n负责实时控制架构",
            "education_text": "浙江大学\n自动化\n本科\n2012.09-2016.06",
            "captured_at": "2026-07-22T10:00:00",
        }

        with patch("xsaas_candidate_search.evaluate", side_effect=[True, {"ready": True, "login": False}, detail]):
            result = capture_candidate_details(object(), [candidate], True)

        self.assertEqual(result["complete"], 1)
        self.assertEqual(candidate["resume_capture_status"], "complete")
        self.assertIn("X-SaaS完整履历", candidate["profile_text"])
        self.assertIn("EtherCAT", candidate["work_text"])

    def test_detail_capture_does_not_promote_list_summary_when_education_is_missing(self) -> None:
        candidate = {
            "xsaas_id": "5566778",
            "profile_text": "列表页工作摘要 " * 20,
            "work_text": "列表页工作经历 " * 10,
            "education_text": "列表页教育摘要",
            "source_url": "https://headhunt.x-saas.com.cn/#/app/candidate/info/5566778",
        }
        detail = {
            "xsaas_id": "5566778",
            "source_url": candidate["source_url"],
            "full_text": "详情页完整履历 " * 20,
            "work_text": "详情页工作经历 " * 10,
            "project_text": "",
            "education_text": "",
            "captured_at": "2026-07-27T10:00:00",
        }

        with patch("xsaas_candidate_search.evaluate", side_effect=[True, {"ready": True, "login": False}, detail]):
            result = capture_candidate_details(object(), [candidate], True)

        self.assertEqual(result["partial"], 1)
        self.assertEqual(candidate["resume_capture_status"], "partial")
        self.assertEqual(candidate["education_text"], "")
        self.assertIn("教育经历", candidate["resume_capture_missing"])

    def test_score_gate_uses_full_resume_and_filters_low_fit(self) -> None:
        candidates = [
            {
                "name": "强匹配", "company": "台达", "title": "电源工程师", "education": "硕士",
                "full_text": "PC 电源 技术市场 TME 多相控制器 DrMOS POL 客户技术推广",
                "resume_capture_status": "complete",
            },
            {
                "name": "弱匹配", "company": "普通公司", "title": "行政专员", "education": "本科",
                "full_text": "行政、人事、办公室管理",
                "resume_capture_status": "complete",
            },
        ]
        profile = type("Profile", (), {})()

        with patch("run_published_position_search.build_db_position_profile", return_value=profile), patch(
            "run_published_position_search.score_candidate_for_profile",
            side_effect=[(82, ["PC电源"], [], "A-优先推荐"), (22, [], ["方向不符"], "D-暂缓")],
        ):
            accepted, stats = apply_position_score_gate(candidates, Path("/tmp/test.db"), "士兰微", "岗位", 55)

        self.assertEqual([item["name"] for item in accepted], ["强匹配"])
        self.assertEqual(candidates[0]["fit_score"], 82)
        self.assertEqual(stats["rejected_low_score"], 1)


if __name__ == "__main__":
    unittest.main()
