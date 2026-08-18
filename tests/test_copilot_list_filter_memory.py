"""会话级名单筛选态（list_filters）回归守护。

背景（2026-08-18）：顾问先"过滤一下给我名单"得到严格筛选名单（17 人），
随后在同一会话说"给我名单/刷新一下"，copilot 按普通名单直答，名单回落
全量（277 人），看起来就像筛选结果丢了。

修复：分级过滤生效后把 {job_id: "grade_filter"} 登记进 business_focus 的
list_filters（随 agent_copilot_focus 持久化）；后续名单直答优先按记忆保持
严格口径；显式"全量名单/不用筛"清除记忆并回落普通名单。

覆盖：
1. 分级过滤轮登记 list_filters，卡片带 filter_mode=grade_filter。
2. 同会话再问"给我名单"不回落，仍走严格筛选。
3. 显式"全量名单"清除记忆，回落普通名单；再问名单不再恢复严格口径。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from test_a_system_agent_v1 import AgentDbCase, fake_assessment  # noqa: E402
from a_system_agent import AgentService, FakeLLM  # noqa: E402
from a_system_agent.copilot_intent import _requests_full_list  # noqa: E402


class FullListIntentTest(unittest.TestCase):
    def test_full_list_markers(self) -> None:
        self.assertTrue(_requests_full_list("给我全量名单"))
        self.assertTrue(_requests_full_list("不用筛了，直接给我名单"))
        self.assertFalse(_requests_full_list("给我名单"))
        self.assertFalse(_requests_full_list("再筛一下给我名单"))


class CopilotListFilterMemoryTest(AgentDbCase):
    def _copilot(self, service: AgentService, message: str) -> dict:
        return service.copilot(
            message,
            session_id="list-filter-memory",
            context={"type": "job", "id": 10, "page": "positions"},
        )

    def test_grade_filter_remembered_until_full_list_requested(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        self.addCleanup(service.close)

        first = self._copilot(service, "把候选池过滤一下，按匹配度给我名单")
        self.assertEqual((first.get("action_card") or {}).get("filter_mode"), "grade_filter")
        focus = service.get_copilot_focus("list-filter-memory") or {}
        self.assertEqual((focus.get("list_filters") or {}).get("10"), "grade_filter")

        # 同会话再问名单：保持严格筛选口径，不回落全量
        second = self._copilot(service, "给我名单")
        self.assertEqual((second.get("action_card") or {}).get("filter_mode"), "grade_filter")

        # 显式要全量：清除记忆，回落普通名单
        third = self._copilot(service, "给我全量名单")
        self.assertFalse((third.get("action_card") or {}).get("filter_mode"))
        focus = service.get_copilot_focus("list-filter-memory") or {}
        self.assertFalse((focus.get("list_filters") or {}).get("10"))

        # 清除后再问名单：维持普通名单
        fourth = self._copilot(service, "给我名单")
        self.assertFalse((fourth.get("action_card") or {}).get("filter_mode"))

    def test_grade_filter_remembered_across_sessions(self) -> None:
        """岗位级口径（job_list_filters）：任何会话做过严格筛选后，全新会话问名单也默认给最新分级名单。

        场景（2026-08-18 copilot_dc34113ddefc）：岗位前一日筛出 17 人分级名单，
        第二天新会话第一句"候选人名单给我"回落全量 287 人。
        """
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        self.addCleanup(service.close)
        context = {"type": "job", "id": 10, "page": "positions"}

        first = service.copilot("把候选池过滤一下，按匹配度给我名单", session_id="session-a", context=context)
        self.assertEqual((first.get("action_card") or {}).get("filter_mode"), "grade_filter")

        # 全新会话无会话级记忆：岗位级记忆兜底，仍按最新数据重算分级名单，并附口径提示
        second = service.copilot("给我名单", session_id="session-b", context=context)
        self.assertEqual((second.get("action_card") or {}).get("filter_mode"), "grade_filter")
        self.assertIn("严格筛选口径", str(second.get("answer") or ""))

        # 显式要全量：岗位级记忆一并清除
        third = service.copilot("给我全量名单", session_id="session-b", context=context)
        self.assertFalse((third.get("action_card") or {}).get("filter_mode"))

        # 之后的全新会话：不再恢复严格口径
        fourth = service.copilot("给我名单", session_id="session-c", context=context)
        self.assertFalse((fourth.get("action_card") or {}).get("filter_mode"))


if __name__ == "__main__":
    unittest.main()
