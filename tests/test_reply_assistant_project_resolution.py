from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "scripts" / "liepin_workbench_server.py"
CONTENT_PATH = ROOT / "liepin-reply-assistant-extension" / "content.js"


def load_server_module():
    sys.path.insert(0, str(SERVER_PATH.parent))
    spec = importlib.util.spec_from_file_location("liepin_workbench_server_project_resolution_test", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


server = load_server_module()


class ReplyAssistantProjectResolutionTest(unittest.TestCase):
    def test_masked_name_only_history_cannot_auto_switch_project(self) -> None:
        self.assertFalse(
            server.project_lookup_auto_apply(
                57,
                ["候选人姓名可互证", "来自开聊/沟通动作"],
            )
        )

    def test_exact_name_communication_history_can_still_auto_apply(self) -> None:
        self.assertTrue(
            server.project_lookup_auto_apply(
                57,
                ["候选人姓名一致", "来自开聊/沟通动作"],
            )
        )

    def test_im_lookup_uses_enriched_identity_before_name_only_fallback(self) -> None:
        source = CONTENT_PATH.read_text(encoding="utf-8")
        lookup = source.split("async function recentOutreachProjectForCurrentContact", 1)[1].split(
            "function isConcreteProject", 1
        )[0]

        self.assertIn("candidate_title: context.contact?.title", lookup)
        self.assertIn("candidate_profile_text: context.combinedText", lookup)
        enriched = lookup.index("{ ...identityParams, ...safeExtraParams, candidate_name: candidateName }")
        fallback = lookup.index("{ candidate_name: candidateName }")
        self.assertLess(enriched, fallback)


if __name__ == "__main__":
    unittest.main()
