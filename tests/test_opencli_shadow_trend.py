from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "experiments" / "opencli_shadow_trend.py"
SPEC = importlib.util.spec_from_file_location("opencli_shadow_trend", MODULE_PATH)
assert SPEC and SPEC.loader
trend = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trend)


def make_round(
    generated_at: str,
    baseline: dict,
    opencli: dict,
    comparison: dict,
    extra: dict | None = None,
) -> dict:
    payload = {
        "schema_version": 1,
        "generated_at": generated_at,
        "mode": "read_only_no_intake_no_outreach",
        "queries": ["q1"],
        "repeats": 1,
        "baseline": baseline,
        "opencli": opencli,
        "comparison": comparison,
        "migration_gate": {"decision": "keep_existing_executor"},
    }
    if extra:
        payload.update(extra)
    return payload


def engine(success_rate=1.0, duration=5000, completeness=1.0):
    return {
        "runs": 1,
        "successful_runs": 1,
        "success_rate": success_rate,
        "mean_duration_ms": duration,
        "field_completeness": completeness,
    }


def comparison(baseline_stability, opencli_stability, baseline_recall, opencli_recall):
    return {
        "baseline_stability_score": baseline_stability,
        "opencli_stability_score": opencli_stability,
        "baseline_relative_recall": baseline_recall,
        "opencli_relative_recall": opencli_recall,
    }


class OpenCliShadowTrendTest(unittest.TestCase):
    def write_round(self, directory: Path, filename: str, payload: dict) -> None:
        (directory / filename).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def test_aggregates_two_rounds_sorted_with_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            self.write_round(data_dir, "liepin-opencli-ab-r2.json", make_round(
                "2026-07-21T18:00:00",
                engine(duration=9807, completeness=0.6),
                engine(duration=6174, completeness=0.6),
                comparison(1.0, 1.0, 1.0, 1.0),
            ))
            self.write_round(data_dir, "liepin-opencli-ab-r1.json", make_round(
                "2026-07-21T17:00:00",
                engine(duration=9123, completeness=0.6),
                engine(duration=5882, completeness=0.6),
                comparison(1.0, 1.0, 1.0, 1.0),
            ))
            report = trend.aggregate(data_dir)
            self.assertEqual(report["round_count"], 2)
            rounds = report["channels"]["liepin"]["rounds"]
            self.assertEqual(len(rounds), 2)
            self.assertEqual(rounds[0]["generated_at"], "2026-07-21T17:00:00")
            self.assertEqual(rounds[1]["generated_at"], "2026-07-21T18:00:00")
            self.assertEqual(rounds[0]["channel"], "liepin")
            self.assertAlmostEqual(rounds[0]["opencli_speedup_percent"], 35.5, places=1)
            self.assertEqual(rounds[0]["baseline_success_rate"], 1.0)
            self.assertEqual(rounds[0]["opencli_field_completeness"], 0.6)
            self.assertEqual(report["data_window"], ["2026-07-21T17:00:00", "2026-07-21T18:00:00"])
            self.assertTrue(report["redline_ok"])

    def test_strict_gate_semantics_tie_is_not_better(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            self.write_round(data_dir, "liepin-opencli-ab-tie.json", make_round(
                "2026-07-21T17:00:00",
                engine(completeness=0.6), engine(duration=4000, completeness=0.6),
                comparison(1.0, 1.0, 1.0, 1.0),
            ))
            self.write_round(data_dir, "xsaas-opencli-ab-tie.json", make_round(
                "2026-07-21T17:30:00",
                engine(completeness=0.6), engine(duration=4000, completeness=0.6),
                comparison(1.0, 1.0, 1.0, 1.0),
            ))
            report = trend.aggregate(data_dir)
            rounds = report["channels"]["liepin"]["rounds"]
            self.assertEqual(rounds[0]["stability_judgment"], "持平")
            self.assertEqual(rounds[0]["recall_judgment"], "持平")
            self.assertEqual(rounds[0]["field_completeness_judgment"], "持平")
            self.assertEqual(
                report["gate_items"]["stability_strictly_better"]["status"], "未满足"
            )
            self.assertEqual(
                report["gate_items"]["relative_recall_strictly_better"]["status"], "未满足"
            )
            self.assertEqual(
                report["gate_items"]["field_completeness_not_worse"]["status"], "已满足"
            )
            self.assertIn("keep_existing_executor", report["decision"])

            self.write_round(data_dir, "xsaas-opencli-ab-better.json", make_round(
                "2026-07-21T18:00:00",
                engine(completeness=0.9), engine(duration=3000, completeness=1.0),
                comparison(0.8, 0.95, 0.9, 1.0),
            ))
            self.write_round(data_dir, "liepin-opencli-ab-better.json", make_round(
                "2026-07-21T19:00:00",
                engine(completeness=0.9), engine(duration=3000, completeness=1.0),
                comparison(0.8, 0.95, 0.9, 1.0),
            ))
            better = trend.aggregate(data_dir)
            liepin_rounds = better["channels"]["liepin"]["rounds"]
            self.assertEqual(liepin_rounds[1]["stability_judgment"], "更优")
            self.assertEqual(liepin_rounds[1]["recall_judgment"], "更优")
            self.assertEqual(liepin_rounds[1]["field_completeness_judgment"], "更优")
            self.assertEqual(
                better["gate_items"]["stability_strictly_better"]["status"], "未满足"
            )
            self.assertEqual(
                better["gate_items"]["reuse_asa_intake_audit"]["status"], "数据不足"
            )
            self.assertEqual(
                better["gate_items"]["independent_pilot_first"]["status"], "需书面评估"
            )

    def test_redline_scan_flags_forbidden_fields_without_copying_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            dirty = make_round(
                "2026-07-21T17:00:00",
                engine(), engine(duration=4000),
                comparison(1.0, 1.0, 1.0, 1.0),
                extra={
                    "auth_bridge": {
                        "ok": True,
                        "webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/page/SECRET",
                    },
                    "candidates": [
                        {
                            "name": "张三",
                            "resumeId": "ext-id-123",
                            "url": "https://example.test/resume/ext-id-123",
                            "full_text": "十年电源工程师经历……",
                        }
                    ],
                },
            )
            self.write_round(data_dir, "liepin-opencli-ab-dirty.json", dirty)
            report = trend.aggregate(data_dir)
            self.assertFalse(report["redline_ok"])
            categories = {finding["category"] for finding in report["redline_findings"]}
            self.assertIn("姓名", categories)
            self.assertIn("外部ID", categories)
            self.assertIn("URL", categories)
            self.assertIn("简历正文", categories)
            self.assertIn("CDP会话值", categories)
            markdown = trend.render_markdown(report)
            self.assertIn("⚠ 告警", markdown)
            self.assertNotIn("张三", markdown)
            self.assertNotIn("ext-id-123", markdown)
            self.assertNotIn("SECRET", markdown)

    def test_single_round_report_marks_only_one_round(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            self.write_round(data_dir, "xsaas-opencli-ab-only.json", make_round(
                "2026-07-21T18:00:00",
                engine(), engine(duration=3743),
                comparison(1.0, 1.0, 1.0, 1.0),
            ))
            report = trend.aggregate(data_dir)
            self.assertEqual(report["round_count"], 1)
            self.assertIn("仅一轮", report["channels"]["xsaas"]["trend"])
            markdown = trend.render_markdown(report)
            self.assertIn("仅一轮", markdown)
            self.assertIn("X-SaaS", markdown)
            self.assertTrue(report["redline_ok"])


if __name__ == "__main__":
    unittest.main()
