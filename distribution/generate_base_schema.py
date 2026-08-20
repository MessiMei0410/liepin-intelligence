#!/usr/bin/env python3
"""重新生成 distribution/base_schema.sql（ASA v3 基座 schema，空库初始化用）。

原理：Core/Agent 的运行时代码（asa_core migrate、a_system_agent ensure_schema、
scheduler、record_*/sync_* 等 ensure 函数）会用 CREATE ... IF NOT EXISTS 自建
一批表/视图；v3 基座表（positions/job_candidates/candidate_events/people/jobs…）
则由仓库外的 v3 流水线创建，仓库内没有建表语句。本脚本取一份现有主库，
从 sqlite_master 中剔除运行时代码自建的对象，剩下的 DDL 即基座 schema。

用法（在主仓根目录执行，仅维护者需要；同事的安装包已含生成结果）：
    python3 distribution/generate_base_schema.py [主库.db]
默认主库路径取环境变量 A_SYSTEM_DB。
"""
from __future__ import annotations

import glob
import inspect
import os
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
OUT = Path(__file__).resolve().parent / "base_schema.sql"

# 主库里的历史垃圾表（备份残留，不进基座）
JUNK = {"candidates_old", "candidate_events_orphaned"}


def runtime_created_objects() -> set[str]:
    """扫描运行时代码里所有 CREATE 语句，收集其自建对象名。"""
    sys.path.insert(0, str(SCRIPTS))
    import a_system_agent.schema as agent_schema  # noqa: PLC0415
    from asa_core.database import MIGRATIONS  # noqa: PLC0415

    sources = [agent_schema.SCHEMA, inspect.getsource(agent_schema.ensure_schema)]
    sources += [sql for _, _, sql in MIGRATIONS]
    files = (
        [SCRIPTS / "liepin_workbench_server.py", SCRIPTS / "ensure_project_confirmation_schema.py"]
        + glob.glob(str(SCRIPTS / "asa_core" / "*.py"))
        + glob.glob(str(SCRIPTS / "a_system_agent" / "*.py"))
        + glob.glob(str(SCRIPTS / "record_*.py"))
        + glob.glob(str(SCRIPTS / "sync_*.py"))
    )
    sources += [Path(f).read_text(encoding="utf-8") for f in files]
    names: set[str] = set()
    for src in set(sources):
        for m in re.finditer(
            r"CREATE\s+(?:VIRTUAL\s+)?(?:TABLE|VIEW|INDEX|TRIGGER)\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_]\w*)",
            src,
            re.I,
        ):
            names.add(m.group(1))
    names |= {"schema_migrations", "sqlite_sequence"}
    return names


def main() -> int:
    db = Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("A_SYSTEM_DB", "")).expanduser()
    if not db.exists():
        print(f"主库不存在：{db}（传参或设 A_SYSTEM_DB）", file=sys.stderr)
        return 1
    runtime = runtime_created_objects()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    objs = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
    conn.close()

    cand = {
        n: (t, tb, sql)
        for t, n, tb, sql in objs
        if n not in runtime and n not in JUNK and not n.startswith("sqlite_")
    }
    base_tables = {n for n, (t, _, _) in cand.items() if t == "table"}

    keep: list[tuple[str, str, str]] = []
    dropped: list[tuple[str, str, str]] = []
    for n, (t, tb, sql) in cand.items():
        # 索引/触发器若挂在运行时自建的表上，随表一起由运行时创建，剔除
        if t in ("index", "trigger") and tb not in base_tables:
            dropped.append((t, n, tb))
            continue
        keep.append((t, n, sql))
    # 视图两轮过滤：引用运行时自建表/被剔除视图的，剔除（由对应运行时代码重建）
    for _ in range(3):
        kept_names = {n for _, n, _ in keep}
        known = {x.lower() for x in base_tables} | {x.lower() for x in kept_names}
        nxt = []
        for t, n, sql in keep:
            if t == "view":
                refs = {m.group(1).lower() for m in re.finditer(r"(?:FROM|JOIN)\s+([A-Za-z_]\w*)", sql, re.I)}
                if not refs <= known:
                    dropped.append((t, n, "refs runtime objects"))
                    continue
            nxt.append((t, n, sql))
        if len(nxt) == len(keep):
            break
        keep = nxt

    order = {"table": 0, "index": 1, "trigger": 1, "view": 2}
    keep.sort(key=lambda o: (order.get(o[0], 3), o[1]))
    lines = [
        "-- ASA v3 基座 schema（空库初始化用，仅 DDL 无数据）",
        "-- 由主库 sqlite_master 差集生成：distribution/generate_base_schema.py",
        "",
    ]
    for t, n, sql in keep:
        sql2 = re.sub(r"^CREATE\s+(TABLE|TRIGGER|VIEW)\s+", r"CREATE \1 IF NOT EXISTS ", sql.strip(), flags=re.I)
        sql2 = re.sub(
            r"^CREATE\s+(UNIQUE\s+)?INDEX\s+",
            lambda m: f"CREATE {m.group(1) or ''}INDEX IF NOT EXISTS ",
            sql2,
            flags=re.I,
        )
        lines.append(sql2 + ";\n")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"已生成 {OUT}：保留 {len(keep)} 个对象，剔除 {len(dropped)} 个（运行时自建/引用运行时表）")
    for t, n, why in dropped:
        print(f"  剔除 {t} {n} ({why})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
