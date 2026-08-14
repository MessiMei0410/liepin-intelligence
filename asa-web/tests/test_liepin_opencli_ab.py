from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


# opencli 实验模块顶层 import 依赖本机 liepin-intelligence 根路径下的
# scripts/xsaas_candidate_search.py；CI（ubuntu）无该路径，模块级跳过，
# 避免 discover 收集时 ImportError 让契约测试整体变红。
LOCAL_SCRIPTS = Path("/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence/scripts/xsaas_candidate_search.py")
if not LOCAL_SCRIPTS.is_file():
    raise unittest.SkipTest("本机 xsaas_candidate_search 缺失（CI ubuntu），跳过 opencli 契约测试")


MODULE_PATH = Path(__file__).resolve().parents[1] / "experiments" / "liepin_opencli_ab.py"
ADAPTER_PATH = Path(__file__).resolve().parents[1] / "opencli" / "clis" / "liepin" / "candidate-search.js"
SPEC = importlib.util.spec_from_file_location("liepin_opencli_ab", MODULE_PATH)
assert SPEC and SPEC.loader
ab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ab)


class LiepinOpenCliAbTest(unittest.TestCase):
    def test_normalizes_both_engine_shapes(self) -> None:
        baseline = ab.normalize_candidate({"name": "张**", "company": "A", "title": "FAE"})
        opencli = ab.normalize_candidate({"name": "张**", "currentCompany": "A", "currentTitle": "FAE"})
        self.assertEqual(ab.candidate_key(baseline), ab.candidate_key(opencli))

    def test_live_report_contains_no_raw_target_id(self) -> None:
        self.assertEqual(ab.public_key("target"), ab.public_key("target"))
        self.assertNotEqual(ab.public_key("target"), "target")

    def test_adapter_exposes_resume_identity_and_marks_list_rows_as_recall(self) -> None:
        source = ADAPTER_PATH.read_text(encoding="utf-8")
        self.assertIn("data-tlg-ext", source)
        self.assertIn("res_id_encode", source)
        for field in ("resumeId", "url", "workText", "educationText"):
            self.assertIn(f"'{field}'", source)
        self.assertIn("dataStage: 'recall'", source)
        self.assertIn("resumeCaptureStatus: 'not_requested'", source)

    def test_normalizer_keeps_resume_link_and_detail_fields(self) -> None:
        candidate = ab.normalize_candidate({
            "resumeId": "r1", "url": "https://example.test/r1",
            "workText": "A · FAE", "educationText": "School",
        })
        self.assertEqual(candidate["candidate_id"], "r1")
        self.assertEqual(candidate["url"], "https://example.test/r1")
        self.assertEqual(candidate["work_text"], "A · FAE")


if __name__ == "__main__":
    unittest.main()
