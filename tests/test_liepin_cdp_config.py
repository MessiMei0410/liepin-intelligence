from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


class LiepinCdpConfigTest(unittest.TestCase):
    def test_profile_is_shared_by_environment(self) -> None:
        import liepin_cdp_config

        with patch.dict(os.environ, {"A_SYSTEM_CDP_PROFILE_DIR": "/tmp/asa-liepin-profile"}, clear=False):
            self.assertEqual(liepin_cdp_config.cdp_profile_dir(), Path("/tmp/asa-liepin-profile"))

    def test_launch_agent_label_follows_configured_plist(self) -> None:
        import liepin_cdp_config

        with patch.dict(os.environ, {"A_SYSTEM_CDP_LAUNCH_AGENT": "/tmp/ai.a-system.chrome-cdp.plist"}, clear=False):
            self.assertEqual(liepin_cdp_config.cdp_launch_agent_label(), "ai.a-system.chrome-cdp")

    def test_opencli_extension_path_can_be_overridden(self) -> None:
        import liepin_cdp_config

        with patch.dict(os.environ, {"A_SYSTEM_OPENCLI_EXTENSION_DIR": "/tmp/opencli-extension"}, clear=False):
            self.assertEqual(liepin_cdp_config.opencli_extension_dir(), Path("/tmp/opencli-extension"))

    def test_liepin_login_probe_rejects_login_page(self) -> None:
        import opencli_sourcing_shadow as shadow

        class FakeCdp:
            def __init__(self, _endpoint: str) -> None:
                pass

            def send(self, _method: str, _params: dict) -> dict:
                return {"result": {"result": {"value": '{"href":"https://h.liepin.com/login","title":"登录","body":"手机号登录"}'}}}

            def close(self) -> None:
                pass

        with patch.object(shadow, "CDP", FakeCdp):
            self.assertFalse(shadow.liepin_tab_is_authenticated({"webSocketDebuggerUrl": "ws://test"}))

    def test_liepin_login_probe_accepts_search_page(self) -> None:
        import opencli_sourcing_shadow as shadow

        class FakeCdp:
            def __init__(self, _endpoint: str) -> None:
                pass

            def send(self, _method: str, _params: dict) -> dict:
                return {"result": {"result": {"value": '{"href":"https://h.liepin.com/search/getConditionItem","title":"找简历","body":"候选人搜索"}'}}}

            def close(self) -> None:
                pass

        with patch.object(shadow, "CDP", FakeCdp):
            self.assertTrue(shadow.liepin_tab_is_authenticated({"webSocketDebuggerUrl": "ws://test"}))
