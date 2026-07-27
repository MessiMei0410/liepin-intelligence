from __future__ import annotations

import unittest
from pathlib import Path


ASA_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_RUNNER = Path(
    "/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence/scripts/xsaas_candidate_search.py"
)
OPENCLI_ADAPTER = ASA_ROOT / "opencli" / "clis" / "xsaas" / "candidate-search.js"


class XsaasSearchParserRegressionTest(unittest.TestCase):
    def _readers(self) -> list[Path]:
        """仓内适配器必查；生产 runner 仅在本机存在时并入（缺失见 test_production_runner_present_or_skipped）。"""
        paths = [OPENCLI_ADAPTER]
        if PRODUCTION_RUNNER.exists():
            paths.insert(0, PRODUCTION_RUNNER)
        return paths

    def test_production_runner_present_or_skipped(self) -> None:
        if not PRODUCTION_RUNNER.exists():
            self.skipTest(f"生产 runner 不在本机，降级为仅仓内适配器断言: {PRODUCTION_RUNNER}")
        self.assertTrue(PRODUCTION_RUNNER.read_text(encoding="utf-8").strip())

    def test_both_readers_support_linkless_angular_rows(self) -> None:
        for path in self._readers():
            source = path.read_text(encoding="utf-8")
            self.assertIn("angular.element(row).scope()?.candidate", source)
            self.assertIn("ipersonid", source)
            self.assertIn("arrJobDetail", source)
            self.assertIn("scompany", source)
            self.assertIn("sposition", source)

    def test_candidate_url_is_reconstructed_from_person_id(self) -> None:
        for path in self._readers():
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

    def test_production_runner_resets_conditions_between_queries(self) -> None:
        """round3/5/7/8 四次实证：X-SaaS 已选条件留在 SPA 内存态逐组累加（AND），
        hash 跳转与 location.reload 均不可靠清零；每组独立克隆标签页才是确定性重置。防回归。"""
        if not PRODUCTION_RUNNER.exists():
            self.skipTest(f"生产 runner 不在本机: {PRODUCTION_RUNNER}")
        source = PRODUCTION_RUNNER.read_text(encoding="utf-8")
        loop = source.index("for index, query in enumerate(queries[:8]):")
        clone = source.index("clone_authenticated_tab(port, source)", loop)
        submit = source.index("SEARCH_JS", loop)
        self.assertLess(loop, clone)
        self.assertLess(clone, submit)
        self.assertIn("wait_for_list(cdp)", source[loop:submit])
        self.assertNotIn("location.reload()", source[loop:submit])
        # 渲染等待：固定 sleep 会读加载中间态（MPS 165 条实证），必须轮询 hasCount && !loading
        self.assertIn("SETTLE_JS", source)
        self.assertIn("settle_timeout", source)
        settle_loop = source.index("settled = False", loop)
        extract = source.index("EXTRACT_JS) or {}", loop)
        self.assertLess(settle_loop, extract)
        # 任务卡 UX-1 问题 B 竞态硬门：渲染完成信号必须与本轮关键词绑定（queryMatch），
        # 防止默认列表/上一轮结果被当作本轮结果（串词错配）。
        self.assertIn("queryMatch", source)
        self.assertIn("query_matches(query, selected)", source)
        # 超时兜底 45s（9f5299c：技术市场大结果集实测 >20s，round10 超时误弃后放宽）
        # + 该词重试一次，仍失败记日志并标记"跳过"（skipped），不得静默丢失。
        self.assertIn("time.time() + 45", source[settle_loop:extract])
        self.assertIn("attempts < 2", source[loop:])
        self.assertIn('"skipped"', source[loop:])
        self.assertIn("file=sys.stderr", source[loop:])


if __name__ == "__main__":
    unittest.main()
