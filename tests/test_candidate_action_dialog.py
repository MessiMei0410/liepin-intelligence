from __future__ import annotations

import unittest
from pathlib import Path


MAIN_SOURCE = Path(__file__).resolve().parents[1] / "src" / "main.tsx"


class CandidateActionDialogRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = MAIN_SOURCE.read_text(encoding="utf-8")

    def test_candidate_actions_do_not_depend_on_browser_confirm(self) -> None:
        self.assertNotRegex(self.source, r"\b(?:window\.)?confirm\s*\(")
        self.assertIn('role="alertdialog"', self.source)

    def test_preflight_token_is_committed_from_custom_dialog(self) -> None:
        self.assertIn("setPendingAction({action,token:pre.token", self.source)
        self.assertIn("api.commit(value.id,action,token,actionNote.trim())", self.source)

    def test_success_refreshes_detail_and_surfaces_feedback(self) -> None:
        self.assertIn("await changed()", self.source)
        self.assertIn("候选人状态已更新", self.source)


if __name__ == "__main__":
    unittest.main()
