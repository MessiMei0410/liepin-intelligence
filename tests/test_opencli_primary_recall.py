from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "opencli_sourcing_shadow.py"
SPEC = importlib.util.spec_from_file_location("opencli_sourcing_shadow", SCRIPT)
assert SPEC and SPEC.loader
shadow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shadow)

from a_system_agent.capability_runtime import RecruitingCapabilityRuntime

MULTICHANNEL = Path("/Users/messi/.codex/skills/multi-channel-search/scripts/a_system_multichannel.py")


def _row(candidate_id: str, name: str, status: str, **extra):
    return {
        "candidate_id": candidate_id,
        "resume_id": candidate_id,
        "name": name,
        "company": "示例公司",
        "title": "工程师",
        "experience": "5年",
        "education": "本科",
        "city": "上海",
        "profile_text": "列表摘要",
        "full_text": "完整履历" * 30 if status == "complete" else "",
        "work_text": "工作经历" * 6 if status == "complete" else "",
        "project_text": "",
        "education_text": "教育经历" * 4 if status == "complete" else "",
        "url": f"https://example.test/{candidate_id}",
        "query": "",
        "data_stage": "detail" if status == "complete" else "recall",
        "resume_capture_status": status,
        "resume_capture_missing": [],
        "resume_capture_error": "",
        "resume_captured_at": "",
        **extra,
    }


class OpenCliPrimaryRecallTest(unittest.TestCase):
    def test_primary_flag_defaults_off_and_is_explicit_opt_in(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(RecruitingCapabilityRuntime._opencli_primary_enabled({}))
            self.assertFalse(RecruitingCapabilityRuntime._opencli_primary_enabled({"opencli_primary": "0"}))
            self.assertTrue(RecruitingCapabilityRuntime._opencli_primary_enabled({"opencli_primary": True}))
        with patch.dict(os.environ, {"ASA_OPENCLI_PRIMARY": "1"}):
            self.assertTrue(RecruitingCapabilityRuntime._opencli_primary_enabled({}))

    def test_primary_recall_dedupes_and_requires_complete_rows(self) -> None:
        per_query = {
            "q1": [_row("a", "甲", "complete"), _row("b", "乙", "failed")],
            "q2": [_row("a", "甲", "complete"), _row("c", "丙", "complete")],
        }
        with patch.object(
            shadow, "run_opencli",
            side_effect=lambda _ch, query, *_args: ([dict(item) for item in per_query[query]], {}),
        ), patch.object(shadow, "apply_position_score_gate", side_effect=lambda rows, *_args: rows):
            summary = shadow.run_primary_recall(
                "liepin", ["q1", "q2"], 24, 9223, Path("/tmp/opencli"),
                Path("/tmp/db.sqlite"), "客户", "岗位", 55, 3, 24,
            )
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["queries_succeeded"], 2)
        self.assertEqual(summary["rows_recalled"], 4)
        self.assertEqual(summary["rows_after_dedupe"], 3)
        self.assertEqual(summary["rows_complete"], 2)
        self.assertEqual([item["candidate_id"] for item in summary["rows"]], ["a", "b", "c"])
        self.assertEqual(summary["rows"][0]["query"], "q1")
        self.assertEqual(summary["rows"][0]["channel"], "liepin")

    def test_primary_recall_not_ok_when_no_complete_rows(self) -> None:
        with patch.object(
            shadow, "run_opencli",
            side_effect=lambda *_args: ([_row("a", "甲", "failed")], {}),
        ), patch.object(shadow, "apply_position_score_gate", side_effect=lambda rows, *_args: rows):
            summary = shadow.run_primary_recall(
                "xsaas", ["q1"], 24, 9223, Path("/tmp/opencli"),
                Path("/tmp/db.sqlite"), "客户", "岗位", 55, 3, 24,
            )
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["rows_complete"], 0)

    def test_primary_recall_records_blocked_queries_and_continues(self) -> None:
        def run(_channel, query, *_args):
            if query == "bad":
                raise RuntimeError("LIEPIN_LOGIN_REQUIRED: no signed-in tab")
            return ([_row("a", "甲", "complete")], {})

        with patch.object(shadow, "run_opencli", side_effect=run), patch.object(
            shadow, "apply_position_score_gate", side_effect=lambda rows, *_args: rows
        ):
            summary = shadow.run_primary_recall(
                "xsaas", ["bad", "good"], 24, 9223, Path("/tmp/opencli"),
                Path("/tmp/db.sqlite"), "客户", "岗位", 55, 3, 24,
            )
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["queries_attempted"], 2)
        self.assertEqual(summary["queries_succeeded"], 1)
        self.assertEqual(summary["blocked"][0]["query"], "bad")
        self.assertIn("LIEPIN_LOGIN_REQUIRED", summary["blocked"][0]["error"])

    def test_main_primary_writes_rows_and_summary_without_candidate_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            queries = root / "queries.json"
            queries.write_text(json.dumps({"queries": ["q1"]}), encoding="utf-8")
            output = root / "primary.json"
            argv = [
                "opencli_sourcing_shadow.py", "--mode", "primary", "--channel", "xsaas",
                "--queries-json", str(queries), "--output", str(output),
                "--client", "客户", "--job", "岗位", "--port", "9223", "--limit", "12",
            ]
            with patch.object(
                shadow, "run_opencli",
                side_effect=lambda *_args: ([_row("x1", "机密姓名", "complete")], {}),
            ), patch.object(
                shadow, "apply_position_score_gate", side_effect=lambda rows, *_args: rows
            ), patch.object(sys, "argv", argv):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    self.assertEqual(shadow.main(), 0)
            rows = json.loads(output.read_text(encoding="utf-8"))
            summary_text = buffer.getvalue()
            summary = json.loads(summary_text)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["rows_written"], 1)
        self.assertEqual(rows[0]["channel"], "xsaas")
        self.assertEqual(rows[0]["query"], "q1")
        self.assertNotIn("机密姓名", summary_text)

    def test_primary_rows_pass_production_intake_normalize(self) -> None:
        if not MULTICHANNEL.exists():
            self.skipTest(f"入库脚本不在本机: {MULTICHANNEL}")
        spec = importlib.util.spec_from_file_location("a_system_multichannel", MULTICHANNEL)
        assert spec and spec.loader
        multichannel = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(multichannel)

        raw = _row("resume-1", "甲", "complete", channel="liepin", query="电源 硬件")
        staged = multichannel.normalize_candidate(
            "liepin", raw, {"client": "客户", "job": "岗位", "job_id": 1},
        )
        self.assertEqual(staged["source_url"], "https://example.test/resume-1")
        self.assertEqual(staged["source_query"], "电源 硬件")
        self.assertEqual(staged["stage"], "S1 新增寻访/待复核")

    def test_runtime_primary_path_keeps_fallback_and_audit_markers(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "a_system_agent" / "capability_runtime.py"
        ).read_text(encoding="utf-8")
        self.assertIn("production_fallback", source)
        self.assertIn("recall_engine", source)
        self.assertIn("_attempt_opencli_primary", source)
        self.assertIn("skip_channels", source)
        body = source[source.index("def _attempt_opencli_primary"):]
        body = body[: body.index("\n    def ") if "\n    def " in body else len(body)]
        self.assertIn('"--mode", "primary"', body)
        self.assertNotIn("intake", body)


if __name__ == "__main__":
    unittest.main()
