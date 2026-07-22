from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SERVER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "liepin_workbench_server.py"
sys.path.insert(0, str(SERVER_PATH.parent))
spec = importlib.util.spec_from_file_location("liepin_workbench_server", SERVER_PATH)
server = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(server)


class HeaderRequest:
    def __init__(self, origin: str | None) -> None:
        self.headers = {} if origin is None else {"Origin": origin}


class CodexSourcingBridgeTest(unittest.TestCase):
    def test_only_local_file_or_no_origin_can_start_sourcing(self) -> None:
        self.assertTrue(server.sourcing_origin_allowed(HeaderRequest(None)))
        self.assertTrue(server.sourcing_origin_allowed(HeaderRequest("null")))
        self.assertFalse(server.sourcing_origin_allowed(HeaderRequest("https://example.com")))

    def test_prompt_scopes_search_without_candidate_outreach(self) -> None:
        prompt = server.sourcing_prompt("长越科技", "机械高级工程师", "run_test")
        self.assertIn("multi-channel-search", prompt)
        self.assertIn("真实搜索", prompt)
        self.assertIn("不得自动发送消息、开聊、推荐岗位或触达候选人", prompt)

    def test_codex_node_runtime_is_added_to_child_path(self) -> None:
        source = SERVER_PATH.read_text(encoding="utf-8")
        self.assertIn('codex_node_dir = str(CODEX_BIN.parent)', source)
        self.assertIn('child_env["PATH"] = codex_node_dir', source)
        self.assertIn("env=child_env", source)

    def test_overview_restores_running_state_after_switching_cards(self) -> None:
        builder = Path("/Users/messi/Documents/Codex/2026-06-26/re/work/build_talent_workbench.py").read_text(encoding="utf-8")
        self.assertIn("window.overviewSourcingRuns", builder)
        self.assertIn("hydrateOverviewSourcingAction(actions[index])", builder)
        self.assertIn("hydrateOverviewSourcingAction(actions[0])", builder)
        self.assertIn("overviewSourcingGoal(action)", builder)
        self.assertIn("'/api/agent/goals'", builder)
        self.assertIn("continueGoalSourcing", builder)
        self.assertNotIn("workbenchPost('/api/sourcing-run'", builder)

    def test_liveness_marks_missing_process_as_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            server, "SOURCING_RUN_DIR", Path(temp_dir)
        ):
            run = {
                "run_id": "run_missing",
                "client": "长越科技",
                "job": "机械高级工程师",
                "status": "running",
                "pid": 99999999,
                "created_at": "2026-07-13T00:00:00",
            }
            server.write_sourcing_run_state(run)
            refreshed = server.refresh_sourcing_run_liveness(run)
            self.assertEqual(refreshed["status"], "interrupted")
            persisted = server.latest_sourcing_run("长越科技", "机械高级工程师")
            self.assertEqual(persisted["status"], "interrupted")

    def test_running_status_extracts_codex_thread_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            server, "SOURCING_RUN_DIR", Path(temp_dir)
        ), mock.patch.object(server, "process_is_alive", return_value=True):
            log_path = Path(temp_dir) / "run_live.jsonl"
            log_path.write_text(
                '{"type":"thread.started","thread_id":"thread-123"}\n',
                encoding="utf-8",
            )
            run = {
                "run_id": "run_live",
                "client": "长越科技",
                "job": "机械高级工程师",
                "status": "running",
                "pid": 123,
                "thread_id": "",
                "log_path": str(log_path),
                "created_at": "2026-07-13T00:00:00",
            }
            server.write_sourcing_run_state(run)
            refreshed = server.refresh_sourcing_run_liveness(run)
            self.assertEqual(refreshed["thread_id"], "thread-123")

    def test_dry_run_validates_canonical_a_system_position(self) -> None:
        state = server.WorkbenchState(
            server.TALENT_DB, server.DEFAULT_OUTPUT_DIR, "127.0.0.1", 8765
        )
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            server, "SOURCING_RUN_DIR", Path(temp_dir)
        ):
            result = server.start_sourcing_run(
                state,
                {
                    "client": "长越科技",
                    "job": "机械高级工程师",
                    "write": False,
                },
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["context"]["position_id"], 127)


if __name__ == "__main__":
    unittest.main()
