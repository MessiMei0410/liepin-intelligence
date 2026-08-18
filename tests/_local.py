"""本机依赖路径解析与守卫（CI 可移植性）。

后端测试涉及三类本机资源：
- 1.5GB 正式库副本（SOURCE_DB，只读复制来源）
- 2026-06-26 固化目录脚本（build_talent_workbench.py / talent_system_sync.py）
- ~/.codex/skills 下 skill 脚本（a-system-workbench / multi-channel-search / liepin-cdp-search）

CI（ubuntu）上这些都不存在。约定：
- 路径一律经 env_path() 解析，可用环境变量覆盖（如 ASA_SOURCE_DB / ASA_BUILDER_PATH）
- 需要本机资源的测试/模块用 require_local() 显式 SkipTest，而不是靠 --ignore 静默排除
- 仓库内文件（asa-web 子树 / scripts/）直接用 REPO_ROOT 相对路径，CI 上照常运行
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ASA_WEB = REPO_ROOT / "asa-web"

SOURCE_DB_DEFAULT = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
BUILDER_PATH_DEFAULT = Path("/Users/messi/Documents/Codex/2026-06-26/re/work/build_talent_workbench.py")
SYNC_PATH_DEFAULT = Path("/Users/messi/Documents/Codex/2026-06-26/re/work/talent_system_sync.py")
A_SYSTEM_WORKBENCH_SCRIPTS_DEFAULT = Path("/Users/messi/.codex/skills/a-system-workbench/scripts")
MULTICHANNEL_SCRIPTS_DEFAULT = Path("/Users/messi/.codex/skills/multi-channel-search/scripts")
LIEPIN_CDP_SCRIPTS_DEFAULT = Path("/Users/messi/.codex/skills/liepin-cdp-search/scripts")


def env_path(name: str, default: Path) -> Path:
    """环境变量优先，缺省回退到本机默认路径。"""
    raw = os.environ.get(name)
    return Path(raw) if raw else default


def fixture_base_db(source_db: Path | None = None) -> Path:
    """模块级 db fixture 的复制来源：精简测试库存在时优先用它。

    精简库由 scripts/build_slim_test_db.py 从正式库导出（行全保留，仅截断
    测试不读取的审计/快照大 JSON 列），fixture 复制体积 1.9GB → ~120MB。
    缺失时回退正式库本身（行为与之前一致）。CI 上两者都不存在，
    调用方的 require_local(SOURCE_DB, ...) 先行 SkipTest，skip 路径不变。
    """
    source = source_db or env_path("ASA_SOURCE_DB", SOURCE_DB_DEFAULT)
    slim = env_path("ASA_SLIM_TEST_DB", source.with_name(source.stem + "_slim_test.db"))
    return slim if slim.exists() else source


def require_local(path: Path, label: str) -> None:
    """本机资源缺失时整模块/整测试 SkipTest（CI 降级），不再靠 --ignore 静默排除。"""
    if not path.exists():
        raise unittest.SkipTest(f"本机资源缺失（{label}）：{path}")


def skip_unless_local(path: Path, label: str):
    """测试级守卫：返回 unittest.skipUnless 装饰器（单测缺失不影响同模块其他用例）。"""
    return unittest.skipUnless(path.exists(), f"本机资源缺失（{label}）：{path}")
