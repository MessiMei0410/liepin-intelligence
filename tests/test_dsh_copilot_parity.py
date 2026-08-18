from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "scripts" / "asa_dsh_parity.py"
PYTHON = "/usr/local/bin/python3"
PRODUCTION_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DSH_BIN = REPO_ROOT / "dsh" / "node_modules" / ".bin" / "dsh"
DSH_WORKSPACE = Path.home() / ".dsh" / "asa-workspace"

# 真跑一轮约 20–45 分钟（LLM 驱动），留足余量。
HARNESS_TIMEOUT_S = int(os.environ.get("ASA_PARITY_TEST_TIMEOUT_S", "5400"))


@unittest.skipUnless(
    os.environ.get("ASA_PARITY_RUN") == "1",
    "DSH parity 实跑默认跳过：需 ASA_PARITY_RUN=1；会真实调用 DeepSeek API 并依赖本机 "
    "dsh profile（asa-server）/ 正式库只读副本，CI 不跑。",
)
class DshCopilotParityTest(unittest.TestCase):
    """方案 A §4 Phase 3：Copilot vs DSH parity 场景集 + §5 七条意图护栏回归。

    薄封装 scripts/asa_dsh_parity.py：正式库 mode=ro 备份到 /tmp，每个 run 用
    clonefile 克隆副本，起隔离 Core（默认 8892）+ 隔离 DSH 常驻服务器（默认
    8893，ASA_CORE_URL 指向隔离 Core）。写场景比业务表副作用 100% 一致，
    读场景比关键事实语义等价，护栏场景逐条断言。正式库与生产服务零接触。
    """

    def test_parity_suite(self) -> None:
        missing = [
            str(path)
            for path in (HARNESS, Path(PYTHON), PRODUCTION_DB, DSH_BIN, DSH_WORKSPACE / "AGENTS.md")
            if not path.exists()
        ]
        if missing:
            self.skipTest(f"parity 依赖缺失：{missing}")

        with tempfile.TemporaryDirectory(prefix="asa-dsh-parity-test-") as tmpdir:
            report_path = Path(tmpdir) / "parity.json"
            proc = subprocess.run(
                [PYTHON, str(HARNESS), "--out", str(report_path)],
                capture_output=True,
                text=True,
                timeout=HARNESS_TIMEOUT_S,
                cwd=REPO_ROOT,
            )
            tail = "\n".join((proc.stdout or "").splitlines()[-20:])
            if not report_path.exists():
                self.fail(
                    f"harness 未产出报告（exit={proc.returncode}）。\nstdout 尾：\n{tail}\n"
                    f"stderr 尾：\n{os.linesep.join((proc.stderr or '').splitlines()[-20:])}"
                )
            report = json.loads(report_path.read_text(encoding="utf-8"))

        scenarios = report.get("scenarios") or []
        self.assertTrue(scenarios, f"harness 未执行任何场景：{report.get('fatal') or 'unknown'}")
        failed = [
            f"{s['id']}: {'; '.join(s.get('notes') or []) or s.get('dsh', {}).get('error') or s.get('copilot', {}).get('error') or 'failed'}"
            for s in scenarios
            if not s.get("pass")
        ]
        self.assertEqual(
            failed,
            [],
            f"parity 未通过（{len(failed)}/{len(scenarios)}）：\n" + "\n".join(failed) + f"\nstdout 尾：\n{tail}",
        )


if __name__ == "__main__":
    unittest.main()
