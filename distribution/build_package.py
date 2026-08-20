#!/usr/bin/env python3
"""ASA 分发包构建（路 B：同事自部署）。

收集运行 ASA 所需的最小产物到 distribution/dist/ASA-<date>/：

    ASA-<date>/
      app/            Python 运行代码（asa_core + a_system_agent + 扁平模块闭包 + base_schema.sql）
      web/dist/       asa-web 预构建产物（Core 以 /asa-app 伺服）
      dsh/            DSH 编排层（asa-tools/asa-server/profile/bin/launchd，不含 node_modules）
      extensions/     两个 Chrome 扩展源码（手动安装，见 INVENTORY.md）
      launchd/        两个 launchd plist 模板（__ASA_HOME__/__HOME__ 占位符）
      install.sh      同事侧安装入口
      MANIFEST.txt    构建时间 / git 版本 / 内容清单

用法（仓库根目录）：
    python3 distribution/build_package.py              # 校验 asa-web/dist 存在后打包
    python3 distribution/build_package.py --build-web  # 先 npm install && npm run build 再打包
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
DIST_ROOT = REPO / "distribution" / "dist"

# 打包时剔除的噪音
EXCLUDE_NAMES = {".DS_Store", "__pycache__", "node_modules"}
EXCLUDE_SUFFIX = (".bak", ".pyc")

PYTHON_PACKAGES = ["asa_core", "a_system_agent"]
# 入口种子：import 闭包从这些模块开始解析
PYTHON_SEEDS = ["asa_core.app", "liepin_workbench_server"]

DSH_ITEMS = [
    "asa-tools",
    "asa-server",
    "asa-profile",
    "asa-server-profile",
    "bin",
    "launchd",
    "package.json",
    "package-lock.json",
    "README.md",
]

EXTENSIONS = ["liepin-reply-assistant-extension", "xsaas-candidate-assistant-extension"]


def ignored(path: Path) -> bool:
    if path.name in EXCLUDE_NAMES:
        return True
    return any(path.name.endswith(s) or s in path.name for s in EXCLUDE_SUFFIX)


def copy_tree(src: Path, dst: Path) -> int:
    count = 0
    for item in sorted(src.rglob("*")):
        rel = item.relative_to(src)
        if any(ignored(p) for p in (item, *item.parents)):
            continue
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            count += 1
    return count


def python_module_closure() -> list[Path]:
    """从种子模块出发，解析 scripts/ 下的本地 import 闭包（含缩进的延迟 import）。"""
    resolved: set[str] = set()
    queue = list(PYTHON_SEEDS)
    files: set[Path] = set()

    def module_file(mod: str) -> Path | None:
        parts = mod.split(".")
        pkg = SCRIPTS.joinpath(*parts)
        if (pkg / "__init__.py").exists():
            return pkg / "__init__.py"
        flat = SCRIPTS.joinpath(*parts).with_suffix(".py")
        return flat if flat.exists() else None

    import_re = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)", re.M)
    while queue:
        mod = queue.pop()
        if mod in resolved:
            continue
        resolved.add(mod)
        f = module_file(mod)
        if f is None:
            continue  # 第三方或 stdlib
        files.add(f)
        text = f.read_text(encoding="utf-8")
        for m in import_re.finditer(text):
            dep = m.group(1)
            # 相对包内引用（from .xxx 在包文件里）：展开为包名
            queue.append(dep)
        # from . import xxx / from .xxx import 的形式
        if f.parent.name in PYTHON_PACKAGES:
            for m in re.finditer(r"^\s*from\s+\.(\w*)\s+import\s+(.+)$", text, re.M):
                sub, names = m.group(1), m.group(2)
                pkg = f.parent.name
                if sub:
                    queue.append(f"{pkg}.{sub}")
                else:
                    for name in re.split(r"[,\s()]+", names):
                        if name and re.match(r"^[A-Za-z_]\w*$", name):
                            queue.append(f"{pkg}.{name}")
    # 包目录整体带上（闭包可能漏掉纯数据/动态 import 的兄弟模块）
    for pkg in PYTHON_PACKAGES:
        for item in (SCRIPTS / pkg).rglob("*.py"):
            if not ignored(item):
                files.add(item)
    return sorted(files)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--build-web", action="store_true", help="打包前先构建 asa-web（npm install && npm run build）")
    args = parser.parse_args()

    web_dist = REPO / "asa-web" / "dist"
    if args.build_web or not (web_dist / "index.html").exists():
        if not args.build_web:
            print("[build] asa-web/dist 不存在，自动执行 --build-web")
        print("[build] 构建 asa-web（npm install && npm run build）…")
        subprocess.run(["npm", "install", "--no-fund", "--no-audit"], cwd=REPO / "asa-web", check=True)
        subprocess.run(["npm", "run", "build"], cwd=REPO / "asa-web", check=True)
    if not (web_dist / "index.html").exists():
        print("[build] 错误：asa-web/dist/index.html 仍不存在", file=sys.stderr)
        return 1

    stamp = datetime.now().strftime("%Y%m%d")
    pkg = DIST_ROOT / f"ASA-{stamp}"
    if pkg.exists():
        shutil.rmtree(pkg)
    pkg.mkdir(parents=True)

    # 1. Python 代码
    print("[build] 收集 Python 运行代码…")
    app_dir = pkg / "app"
    app_dir.mkdir()
    py_files = python_module_closure()
    for f in py_files:
        rel = f.relative_to(SCRIPTS)
        target = app_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
    shutil.copy2(REPO / "distribution" / "base_schema.sql", app_dir / "base_schema.sql")
    print(f"        {len(py_files)} 个 .py + base_schema.sql")

    # 2. 前端产物
    print("[build] 拷贝 asa-web/dist …")
    copy_tree(web_dist, pkg / "web" / "dist")

    # 3. DSH 编排层
    print("[build] 拷贝 dsh/ …")
    for item in DSH_ITEMS:
        src = REPO / "dsh" / item
        dst = pkg / "dsh" / item
        if src.is_dir():
            copy_tree(src, dst)
        elif src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        else:
            print(f"        警告：dsh/{item} 不存在，跳过")

    # 4. 扩展
    print("[build] 拷贝 Chrome 扩展 …")
    for ext in EXTENSIONS:
        copy_tree(REPO / ext, pkg / "extensions" / ext)

    # 5. launchd 模板 + 安装脚本 + 文档
    copy_tree(REPO / "distribution" / "launchd", pkg / "launchd")
    shutil.copy2(REPO / "distribution" / "install.sh", pkg / "install.sh")
    (pkg / "install.sh").chmod(0o755)
    shutil.copy2(REPO / "distribution" / "INVENTORY.md", pkg / "INVENTORY.md")

    # 6. MANIFEST
    git_sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True
    ).stdout.strip()
    total = sum(1 for p in pkg.rglob("*") if p.is_file())
    (pkg / "MANIFEST.txt").write_text(
        f"ASA 分发包\n构建时间: {datetime.now().isoformat(timespec='seconds')}\n"
        f"git: {git_sha}\n文件数: {total}\n",
        encoding="utf-8",
    )
    print(f"[build] 完成 -> {pkg}（{total} 个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
