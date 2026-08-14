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


MODULE_PATH = Path(__file__).resolve().parents[1] / "experiments" / "xsaas_opencli_ab.py"
ADAPTER_PATH = Path(__file__).resolve().parents[1] / "opencli" / "clis" / "xsaas" / "candidate-search.js"
SPEC = importlib.util.spec_from_file_location("xsaas_opencli_ab", MODULE_PATH)
assert SPEC and SPEC.loader
ab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ab)


def run(engine: str, ids: list[str], *, success: bool = True, duration_ms: int = 1000):
    return {
        "engine": engine,
        "success": success,
        "duration_ms": duration_ms,
        "candidates": [
            {
                "candidate_id": value,
                "name": f"candidate-{value}",
                "company": "company",
                "title": "title",
                "url": f"https://example.test/{value}",
            }
            for value in ids
        ],
    }


class XsaasOpenCliAbTest(unittest.TestCase):
    def test_candidate_key_prefers_channel_id(self) -> None:
        self.assertEqual(ab.candidate_key({"candidate_id": "123", "name": "A"}), "id:123")

    def test_equal_results_do_not_unlock_action_migration(self) -> None:
        baseline = [run("baseline_cdp", ["1", "2"]), run("baseline_cdp", ["1", "2"])]
        opencli = [run("opencli", ["1", "2"]), run("opencli", ["1", "2"])]
        report = ab.compare_runs(baseline, opencli)
        self.assertEqual(report["comparison"]["overlap"], 2)
        self.assertFalse(report["migration_gate"]["stability_better"])
        self.assertFalse(report["migration_gate"]["relative_recall_better"])
        self.assertFalse(report["migration_gate"]["migrate_execution_actions"])

    def test_opencli_must_win_stability_and_relative_recall(self) -> None:
        baseline = [run("baseline_cdp", ["1"]), run("baseline_cdp", [], success=False)]
        opencli = [run("opencli", ["1", "2"]), run("opencli", ["1", "2"])]
        report = ab.compare_runs(baseline, opencli)
        self.assertTrue(report["migration_gate"]["stability_better"])
        self.assertTrue(report["migration_gate"]["relative_recall_better"])
        self.assertTrue(report["migration_gate"]["migrate_execution_actions"])

    def test_successful_empty_search_counts_as_stable_run(self) -> None:
        summary = ab.summarize_runs([run("opencli", [], success=True)])
        self.assertEqual(summary["successful_runs"], 1)
        self.assertEqual(summary["success_rate"], 1.0)
        self.assertEqual(summary["unique_candidates"], 0)

    def test_adapter_marks_list_results_as_recall_data(self) -> None:
        source = ADAPTER_PATH.read_text(encoding="utf-8")
        for field in ("candidateId", "url", "workText", "educationText"):
            self.assertIn(f"'{field}'", source)
        self.assertIn("dataStage: 'recall'", source)
        self.assertIn("resumeCaptureStatus: 'not_requested'", source)


if __name__ == "__main__":
    unittest.main()
