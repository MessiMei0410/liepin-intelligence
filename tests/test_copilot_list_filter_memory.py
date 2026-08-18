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


if __name__ == "__main__":
    unittest.main()
