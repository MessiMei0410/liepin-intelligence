import tempfile
import unittest
from pathlib import Path

from run_published_position_search import (
    CAPTURE_LINKS_JS,
    EXTRACT_JS,
    PositionProfile,
    _split_profile_terms,
    merge_resume_detail,
    score_candidate_for_profile,
    write_report,
)


class PublishedPositionSearchReportTest(unittest.TestCase):
    def test_profile_terms_expand_channel_query_dialects(self) -> None:
        self.assertEqual(
            _split_profile_terms(["PC 电源 TME", "FAE/AE", "design-in/design-win"]),
            ["PC", "电源", "TME", "FAE", "AE", "design-in", "design-win"],
        )

    def test_hard_experience_gate_caps_keyword_rich_candidate(self) -> None:
        profile = PositionProfile(
            slug="test", headline="技术市场经理", default_city="", default_salary="",
            report_title="", file_prefix="", search_rounds=[], target_companies=[],
            core_keywords=["PC", "电源", "TME"], tool_keywords=[], title_keywords=["TME"],
            noise_keywords=[], default_noise_note="", outreach_summary="",
            minimum_experience_years=4, minimum_education="本科",
        )
        score, _evidence, risks, _level = score_candidate_for_profile(
            {
                "raw_text": "PC 电源 TME 客户技术推广", "current_title": "TME",
                "experience": "1年", "education": "本科", "city": "上海", "work": [],
            },
            "上海",
            profile,
        )
        self.assertLessEqual(score, 49)
        self.assertTrue(any("工作年限不足" in risk for risk in risks))

    def test_card_extractor_reads_resume_id_from_tracking_metadata(self) -> None:
        self.assertIn("data-tlg-ext", EXTRACT_JS)
        self.assertIn("res_id_encode", EXTRACT_JS)
        self.assertIn("showresumedetail", EXTRACT_JS)
        self.assertIn("data-tlg-ext", CAPTURE_LINKS_JS)
        self.assertNotIn('split("&index=")', CAPTURE_LINKS_JS)

    def test_report_filename_sanitizes_job_path_separators(self) -> None:
        profile = PositionProfile(
            slug="test",
            headline="技术市场经理（三次电源/服务器或PC市场）",
            default_city="杭州",
            default_salary="",
            report_title="士兰微技术市场经理寻访结果",
            file_prefix="士兰微_技术市场经理（三次电源/服务器或PC市场）_猎聘寻访",
            search_rounds=[],
            target_companies=[],
            core_keywords=[],
            tool_keywords=[],
            title_keywords=[],
            noise_keywords=[],
            default_noise_note="",
            outreach_summary="",
        )
        result = {
            "profile": profile,
            "candidates": [],
            "generated_at": "2026-07-20T14:00:00",
            "client": "士兰微",
            "position": profile.headline,
            "city": "杭州",
            "salary": "",
            "dry_run": True,
        }

        with tempfile.TemporaryDirectory() as folder:
            path = write_report(result, Path(folder))

            self.assertTrue(path.is_file())
            self.assertNotIn("/", path.name)
            self.assertIn("服务器或PC市场", path.name)

    def test_full_resume_detail_replaces_card_summary_before_intake(self) -> None:
        card = {
            "raw_text": "搜索卡片摘要",
            "resume_url": "https://h.liepin.com/resume/showresumedetail/?res_id_encode=abc",
        }
        merged = merge_resume_detail(
            card,
            {
                "source_url": card["resume_url"],
                "full_text": "候选人完整履历 " * 20,
                "work_text": "某半导体有限公司\n2020.01-至今\n产品市场经理\n负责产品定义和客户导入",
                "project_text": "PC电源多相控制器项目\n负责DrMOS方案导入",
                "education_text": "华中科技大学\n电力电子与电力传动\n硕士\n2008.09-2011.03",
                "captured_at": "2026-07-22T10:00:00",
            },
        )

        self.assertEqual(merged["resume_capture_status"], "complete")
        self.assertIn("完整履历", merged["profile_text"])
        self.assertIn("产品市场经理", merged["work_text"])
        self.assertIn("多相控制器", merged["project_text"])
        self.assertIn("华中科技大学", merged["education_text"])


if __name__ == "__main__":
    unittest.main()
