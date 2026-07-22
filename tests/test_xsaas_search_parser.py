from __future__ import annotations

import unittest
from pathlib import Path


ASA_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_RUNNER = Path(
    "/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence/scripts/xsaas_candidate_search.py"
)
OPENCLI_ADAPTER = ASA_ROOT / "opencli" / "clis" / "xsaas" / "candidate-search.js"


class XsaasSearchParserRegressionTest(unittest.TestCase):
    def test_both_readers_support_linkless_angular_rows(self) -> None:
        for path in (PRODUCTION_RUNNER, OPENCLI_ADAPTER):
            source = path.read_text(encoding="utf-8")
            self.assertIn("angular.element(row).scope()?.candidate", source)
            self.assertIn("ipersonid", source)
            self.assertIn("arrJobDetail", source)
            self.assertIn("scompany", source)
            self.assertIn("sposition", source)

    def test_candidate_url_is_reconstructed_from_person_id(self) -> None:
        for path in (PRODUCTION_RUNNER, OPENCLI_ADAPTER):
            source = path.read_text(encoding="utf-8")
            self.assertIn("/app/candidate/info/${personId}", source)

    def test_opencli_waits_for_results_after_submitting_query(self) -> None:
        source = OPENCLI_ADAPTER.read_text(encoding="utf-8")
        submit = source.index("await submitQuery(page, query)")
        settle = source.index("await page.wait({ time: 3.5 })", submit)
        read = source.index("await readResults(page, query)", settle)
        self.assertLess(submit, settle)
        self.assertLess(settle, read)

    def test_opencli_waits_for_list_data_initialization(self) -> None:
        source = OPENCLI_ADAPTER.read_text(encoding="utf-8")
        self.assertIn('tr[ng-repeat="candidate in onePagePerson"]', source)
        self.assertIn("state.ready && state.dataReady", source)

    def test_opencli_does_not_read_rows_while_loading(self) -> None:
        source = OPENCLI_ADAPTER.read_text(encoding="utf-8")
        self.assertIn("loading: bodyText.includes('loading...')", source)
        self.assertIn("result?.queryMatched && !result.loading", source)


if __name__ == "__main__":
    unittest.main()
