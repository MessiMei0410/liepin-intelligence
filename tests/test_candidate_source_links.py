from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from _local import env_path, require_local


BUILDER_PATH = env_path("ASA_BUILDER_PATH", Path("/Users/messi/Documents/Codex/2026-06-26/re/work/build_talent_workbench.py"))
require_local(BUILDER_PATH, "build_talent_workbench.py 脚本")
spec = importlib.util.spec_from_file_location("build_talent_workbench_source_link_test", BUILDER_PATH)
builder = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(builder)


class CandidateSourceLinksTest(unittest.TestCase):
    def test_accepts_resume_url_field_domain(self) -> None:
        links: dict[str, str] = {}
        builder.register_candidate_source_link(
            links,
            "https://h.liepin.com/resume/showresumedetail/?res_id_encode=e26f2580d0T1579b7930027",
        )
        self.assertIn("liepin", links)

    def test_recovers_liepin_link_from_profile_resume_number(self) -> None:
        links: dict[str, str] = {}
        builder.register_liepin_resume_id(links, "中文简历\n简历编号：e565278adcH1f7ab0980e26|最后登录")
        self.assertEqual(
            links["liepin"],
            "https://h.liepin.com/resume/showresumedetail/?res_id_encode=e565278adcH1f7ab0980e26",
        )

    def test_extracts_xsaas_link_from_notes(self) -> None:
        links: dict[str, str] = {}
        builder.register_candidate_source_link(
            links,
            "来源：https://headhunt.x-saas.com.cn/#/app/candidate/info/4681173｜待复核",
        )
        self.assertEqual(links["xsaas"], "https://headhunt.x-saas.com.cn/#/app/candidate/info/4681173")

    def test_compact_list_keeps_source_actions_in_detail_header(self) -> None:
        source = BUILDER_PATH.read_text(encoding="utf-8")
        self.assertNotIn("<th>来源简历</th>", source)
        self.assertIn('class="candidate-compact-list"', source)
        self.assertIn("const sourceActions = candidateSourceActions(c);", source)
        self.assertIn("raw_event.get(\"resume_url\")", source)

    def test_compact_list_does_not_require_horizontal_table_scroll(self) -> None:
        source = BUILDER_PATH.read_text(encoding="utf-8")
        self.assertIn('class="panel candidate-compact-panel"', source)
        self.assertIn(".candidate-compact-list {{ display: grid;", source)
        self.assertIn(".candidate-compact-row {{ width: 100%; display: grid;", source)
        self.assertNotIn('class="candidate-list-table"', source)
        self.assertIn(".candidate-source-actions {{ display: flex; flex-wrap: nowrap;", source)


if __name__ == "__main__":
    unittest.main()
