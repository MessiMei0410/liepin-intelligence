from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "opencli_sourcing_shadow.py"
SPEC = importlib.util.spec_from_file_location("opencli_sourcing_shadow", SCRIPT)
assert SPEC and SPEC.loader
shadow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shadow)

from a_system_agent.capability_runtime import RecruitingCapabilityRuntime


class OpenCliSourcingShadowTest(unittest.TestCase):
    def test_xsaas_identity_prefers_channel_id(self) -> None:
        item = shadow.normalize_candidate("xsaas", {"candidateId": "123", "name": "A"})
        self.assertEqual(shadow.candidate_key("xsaas", item), "id:123")

    def test_liepin_identity_uses_name_company_and_title(self) -> None:
        item = shadow.normalize_candidate(
            "liepin", {"name": "张**", "currentCompany": "A", "currentTitle": "FAE"},
        )
        self.assertEqual(shadow.candidate_key("liepin", item), "identity:张**|a|fae")

    def test_normalize_candidate_preserves_detail_fields_and_capture_state(self) -> None:
        item = shadow.normalize_candidate("liepin", {
            "resumeId": "resume-1",
            "name": "张**",
            "currentCompany": "A",
            "currentTitle": "FAE",
            "url": "https://example.test/resume-1",
            "fullText": "基本信息\n工作经历",
            "workText": "A · FAE",
            "projectText": "项目 A",
            "educationText": "学校 A",
            "dataStage": "detail",
            "resumeCaptureStatus": "complete",
        })
        self.assertEqual(item["candidate_id"], "resume-1")
        self.assertEqual(item["full_text"], "基本信息\n工作经历")
        self.assertEqual(item["work_text"], "A · FAE")
        self.assertEqual(item["education_text"], "学校 A")
        self.assertEqual(item["resume_capture_status"], "complete")

    def test_liepin_detail_capture_reuses_production_capture(self) -> None:
        rows = [shadow.normalize_candidate("liepin", {
            "resumeId": "resume-1", "name": "张**", "currentCompany": "A",
            "currentTitle": "FAE", "url": "https://example.test/resume-1",
            "profileText": "列表摘要",
        })]

        def capture(_port, candidates, _limit):
            candidates[0].update({
                "full_text": "x" * 120,
                "work_text": "工作经历" * 10,
                "education_text": "教育经历" * 5,
                "resume_capture_status": "complete",
            })
            return {"requested": 1, "attempted": 1, "complete": 1, "partial": 0, "failed": 0}

        with patch.object(shadow, "capture_resume_details", side_effect=capture) as mocked:
            result = shadow.capture_opencli_details("liepin", rows, 9223, "unused")
        mocked.assert_called_once()
        self.assertEqual(result["complete"], 1)
        self.assertEqual(rows[0]["recall_profile_text"], "列表摘要")
        self.assertEqual(rows[0]["data_stage"], "detail")

    def test_xsaas_detail_capture_uses_isolated_opencli_target(self) -> None:
        rows = [shadow.normalize_candidate("xsaas", {
            "candidateId": "123", "name": "A", "url": "https://example.test/123",
        })]

        class FakeCDP:
            closed = False

            def __init__(self, endpoint):
                self.endpoint = endpoint

            def close(self):
                self.closed = True

        def capture(cdp, candidates, enabled):
            self.assertEqual(cdp.endpoint, "ws://example.test/target")
            self.assertTrue(enabled)
            candidates[0]["resume_capture_status"] = "partial"
            return {"requested": 1, "complete": 0, "partial": 1, "failed": 0}

        with patch.object(shadow, "CDP", FakeCDP), patch.object(
            shadow, "capture_candidate_details", side_effect=capture,
        ):
            result = shadow.capture_opencli_details("xsaas", rows, 9223, "ws://example.test/target")
        self.assertEqual(result["partial"], 1)
        self.assertEqual(rows[0]["xsaas_id"], "123")
        self.assertEqual(rows[0]["data_stage"], "detail")

    def test_select_baseline_is_restricted_to_the_sample_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "baseline.json"
            path.write_text(json.dumps([
                {"xsaas_id": "1", "name": "A", "query": "q1"},
                {"xsaas_id": "2", "name": "B", "query": "q2"},
            ]), encoding="utf-8")
            rows = shadow.select_baseline("xsaas", path, "q1")
        self.assertEqual([item["candidate_id"] for item in rows], ["1"])

    def test_comparison_contains_only_counts_and_hashed_differences(self) -> None:
        baseline = [shadow.normalize_candidate("xsaas", {"xsaas_id": "1", "name": "A"})]
        candidate = shadow.normalize_candidate("xsaas", {"xsaas_id": "2", "name": "B"})
        result = shadow.compare("xsaas", baseline, [candidate])
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["baseline_only"], 1)
        self.assertEqual(result["shadow_only"], 1)
        self.assertNotIn("id:1", encoded)
        self.assertNotIn("id:2", encoded)

    def test_comparison_reports_resume_completeness_without_candidate_content(self) -> None:
        baseline = [shadow.normalize_candidate("xsaas", {
            "candidateId": "1", "name": "A", "fullText": "完整", "workText": "工作",
            "educationText": "教育", "resumeCaptureStatus": "complete",
        })]
        result = shadow.compare("xsaas", baseline, baseline)
        self.assertEqual(result["baseline_resume_completeness"], 1.0)
        self.assertEqual(result["baseline_capture"]["complete"], 1)
        self.assertNotIn("完整", json.dumps(result, ensure_ascii=False))

    def test_runtime_shadow_is_non_blocking_and_never_affects_intake(self) -> None:
        runtime = RecruitingCapabilityRuntime.__new__(RecruitingCapabilityRuntime)
        runtime.python = "python3"
        runtime.service = SimpleNamespace(db_path=Path("/tmp/test.db"))

        def blocked(_command, _timeout):
            raise RuntimeError("shadow unavailable")

        runtime._run_json = blocked
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            liepin = root / "liepin.json"
            xsaas = root / "xsaas.json"
            liepin.write_text("[]", encoding="utf-8")
            xsaas.write_text("[]", encoding="utf-8")
            result = runtime._run_opencli_shadow(
                request={"workflow_id": "wf-test"},
                client="client",
                job="job",
                port=9223,
                limit=20,
                liepin_queries=[{"query": "q1"}],
                xsaas_queries=[{"query": "q2"}],
                liepin_path=liepin,
                xsaas_path=xsaas,
                artifact_path=root / "shadow.json",
            )
        self.assertTrue(result["enabled"])
        self.assertFalse(result["affects_intake"])
        self.assertFalse(result["affects_outreach"])
        self.assertEqual([item["status"] for item in result["channels"]], ["blocked", "blocked"])

    def test_runtime_shadow_can_be_disabled_explicitly(self) -> None:
        runtime = RecruitingCapabilityRuntime.__new__(RecruitingCapabilityRuntime)
        runtime.python = "python3"
        runtime.service = SimpleNamespace(db_path=Path("/tmp/test.db"))
        result = runtime._run_opencli_shadow(
            request={"opencli_shadow": False},
            client="client",
            job="job",
            port=9223,
            limit=20,
            liepin_queries=[],
            xsaas_queries=[],
            liepin_path=Path("/tmp/liepin.json"),
            xsaas_path=Path("/tmp/xsaas.json"),
            artifact_path=Path("/tmp/shadow.json"),
        )
        self.assertFalse(result["enabled"])
        self.assertFalse(result["affects_intake"])

    def test_opencli_bin_directory_is_added_to_child_path(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('env["PATH"] = str(opencli_bin.parent)', source)

    def test_runtime_missing_query_is_empty_not_string_none(self) -> None:
        self.assertEqual(RecruitingCapabilityRuntime._query_text([{}]), "")


if __name__ == "__main__":
    unittest.main()
