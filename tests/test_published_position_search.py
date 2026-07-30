import json
import tempfile
import unittest
from pathlib import Path

from run_published_position_search import (
    CAPTURE_LINKS_JS,
    EXTRACT_JS,
    PositionProfile,
    _split_profile_terms,
    load_query_rounds,
    merge_resume_detail,
    score_candidate_for_profile,
    technical_market_hard_gate,
    write_report,
)


class PublishedPositionSearchReportTest(unittest.TestCase):
    def test_query_round_loader_preserves_resume_cursor_and_cell_id(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "queries.json"
            path.write_text(json.dumps({"queries": [{
                "cell_id": "qpc_resume",
                "query": "精密 机械",
                "cursor": {"page": 51},
                "collected_before": 1000,
            }]}), encoding="utf-8")

            rounds = load_query_rounds(str(path))

        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0].query, "精密 机械")
        self.assertEqual(rounds[0].start_page, 51)
        self.assertEqual(rounds[0].collected_before, 1000)
        self.assertEqual(rounds[0].filters["cell_id"], "qpc_resume")

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

    def test_pc_power_technical_market_gate_rejects_structure_designer(self) -> None:
        profile = PositionProfile(
            slug="test", headline="技术市场经理/总监（PC电源）", default_city="", default_salary="",
            report_title="", file_prefix="", search_rounds=[], target_companies=["联想"],
            core_keywords=["PC", "电源"], tool_keywords=[], title_keywords=["主管"],
            noise_keywords=[], default_noise_note="", outreach_summary="",
            minimum_experience_years=4, minimum_education="本科",
        )
        score, _evidence, risks, _level = score_candidate_for_profile(
            {
                "raw_text": "联想 ATX PC电源项目，负责机箱结构设计、模具和散热片",
                "current_title": "电源结构设计主管", "experience": "10年", "education": "本科",
                "city": "深圳", "work": [],
            },
            "",
            profile,
        )
        self.assertLessEqual(score, 49)
        self.assertIn("缺少技术市场/FAE/产品定义硬证据", risks)

    def test_pc_power_technical_market_gate_disambiguates_tme(self) -> None:
        profile = PositionProfile(
            slug="test", headline="技术市场经理/总监（PC电源）", default_city="", default_salary="",
            report_title="", file_prefix="", search_rounds=[], target_companies=[],
            core_keywords=["PC", "电源", "TME"], tool_keywords=[], title_keywords=["TME"],
            noise_keywords=[], default_noise_note="", outreach_summary="",
        )
        self.assertEqual(
            technical_market_hard_gate("腾讯音乐娱乐 TME 商务渠道 PC客户端", profile),
            ["缺少PC 电源/多相供电硬证据"],
        )
        self.assertEqual(
            technical_market_hard_gate("PC电源 DrMOS FAE 客户技术推广和产品定义", profile),
            [],
        )

    def test_pc_power_technical_market_gate_keeps_strong_fae_candidate(self) -> None:
        profile = PositionProfile(
            slug="test", headline="技术市场经理/总监（PC电源）", default_city="杭州", default_salary="",
            report_title="", file_prefix="", search_rounds=[], target_companies=["MPS"],
            core_keywords=["PC电源", "DrMOS", "多相控制器", "FAE", "产品定义"],
            tool_keywords=[], title_keywords=["FAE", "技术市场"], noise_keywords=[],
            default_noise_note="", outreach_summary="", minimum_experience_years=4,
            minimum_education="本科",
        )
        score, _evidence, risks, level = score_candidate_for_profile(
            {
                "raw_text": "MPS PC电源 FAE，负责多相控制器和DrMOS产品定义、客户技术推广及design-in",
                "current_title": "高级FAE", "experience": "8年", "education": "硕士",
                "city": "杭州", "work": [],
            },
            "杭州",
            profile,
        )
        self.assertGreaterEqual(score, 65)
        self.assertIn(level, {"A-优先推荐", "B-可沟通"})
        self.assertFalse(any("硬证据" in risk for risk in risks))

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
