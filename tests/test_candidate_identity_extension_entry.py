from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CandidateIdentityExtensionEntryTest(unittest.TestCase):
    def test_xsaas_assistant_exposes_confirmed_merge_flow(self) -> None:
        source = (ROOT / "xsaas-candidate-assistant-extension" / "content.js").read_text(encoding="utf-8")
        self.assertIn("发现同一人", source)
        self.assertIn("对比档案", source)
        self.assertIn("确认合并", source)
        self.assertIn("/api/candidate-identity-matches", source)
        self.assertIn("/api/candidate-merge", source)
        self.assertIn("confirmation_token", source)

    def test_liepin_assistant_exposes_confirmed_merge_flow(self) -> None:
        source = (ROOT / "liepin-reply-assistant-extension" / "content.js").read_text(encoding="utf-8")
        self.assertIn("发现同一人", source)
        self.assertIn("对比档案", source)
        self.assertIn("确认合并", source)
        self.assertIn("/api/candidate-identity-matches", source)
        self.assertIn("/api/candidate-merge", source)
        self.assertIn("confirmation_token", source)

    def test_extension_versions_are_bumped(self) -> None:
        xsaas = json.loads((ROOT / "xsaas-candidate-assistant-extension" / "manifest.json").read_text(encoding="utf-8"))
        liepin = json.loads((ROOT / "liepin-reply-assistant-extension" / "manifest.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(tuple(map(int, xsaas["version"].split("."))), (0, 1, 17))
        self.assertGreaterEqual(tuple(map(int, liepin["version"].split("."))), (0, 3, 1))

    def test_xsaas_blocks_obvious_cross_function_project_mismatch(self) -> None:
        source = (ROOT / "xsaas-candidate-assistant-extension" / "content.js").read_text(encoding="utf-8")
        self.assertIn("function obviousProjectMismatch", source)
        self.assertIn("机械人选不能写入软件岗位", source)
        self.assertIn("软件人选不能写入机械岗位", source)
        self.assertIn("quality.projectMismatch && !quality.missing.length", source)


if __name__ == "__main__":
    unittest.main()
