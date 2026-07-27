#!/usr/bin/env python3
"""S7-3：雷达周度定时扫描 CLI —— 扫描 + 过期降权 + 周报一条龙（radar_weekly_scan）。

口径：docs/TASKCARD_S7-3_雷达定时化_20260727.md（定时化入口：退出码非零即失败；
主代理用 Blueprint Automation 建周度任务调它，扫描窗口建议工作日早 7-8 点）。

退出码契约：0=扫描与周报全部完成；2=任一环节失败（数据库不存在/扫描异常/周报异常，
堆栈打 stderr，定时任务据非零判失败重试）。

红线沿用 radar_scan：公开信息只读、全局限速 ≤1 QPS（min_interval 0.6s + 每公司 2 组查询）、
无来源信号拒写、禁挖照常过滤、不自动触达；Copilot 提醒只推条数和入口（可用 --no-copilot-push 关闭）。
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

EXIT_OK = 0
EXIT_FAILED = 2


def build_service(db_path: str) -> Any:
    from a_system_agent.service import AgentService

    return AgentService(db_path)


def run_pipeline(
    service: Any,
    *,
    max_companies: int = 0,
    max_workers: int = 1,
    push_copilot: bool = True,
) -> dict[str, Any]:
    """扫描（含过期降权/去重合并）→ 周报（含 Copilot 提醒）；任何异常向上抛（main 转非零退出码）。"""
    scan = service.create_radar_scan(max_companies=max_companies, max_workers=max_workers)
    report = service.create_radar_weekly_report(push_copilot=push_copilot)
    return {"scan": scan, "report": report}


def main(argv: list[str] | None = None, *, service_factory: Callable[[], Any] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S7-3 雷达周度扫描+周报一条龙（退出码非零即失败）")
    parser.add_argument("--db", default="", help="SQLite 库路径（缺省取 A_SYSTEM_DB 环境变量/仓内默认库）")
    parser.add_argument("--max-companies", type=int, default=0, help="限量扫描前 N 家（0=全量；调试/验收用）")
    parser.add_argument("--max-workers", type=int, default=1, help="并行扫描线程数（默认 1，限速 ≤1 QPS）")
    parser.add_argument("--no-copilot-push", action="store_true", help="周报生成后不向 Copilot 仲裁层推提醒")
    args = parser.parse_args(argv)

    try:
        if service_factory is not None:
            service = service_factory()
        else:
            from asa_core.database import DEFAULT_DB

            db_path = args.db or str(DEFAULT_DB)
            if not Path(db_path).is_file():
                print(f"数据库不存在：{db_path}", file=sys.stderr)
                return EXIT_FAILED
            service = build_service(db_path)
        outcome = run_pipeline(
            service,
            max_companies=max(0, int(args.max_companies)),
            max_workers=max(1, int(args.max_workers)),
            push_copilot=not args.no_copilot_push,
        )
    except Exception:  # noqa: BLE001 失败一律非零退出（定时任务据此判失败）
        traceback.print_exc()
        return EXIT_FAILED

    scan = outcome["scan"]
    report = outcome["report"]
    stats = (scan.get("radar_scan") or {}).get("stats") or {}
    print(
        f"扫描完成：{scan.get('artifact_id')}（公司 {stats.get('companies_scanned', 0)} 家，"
        f"信号 {stats.get('signals_found', 0)} 条，过期降权 {stats.get('expired_signals', 0)} 条，"
        f"结转 {stats.get('carried_over_signals', 0)} 条，失败留痕 {stats.get('sources_failed', 0)} 次）"
    )
    print(f"周报完成：{report.get('artifact_id')} → {report.get('report_file')}")
    copilot = report.get("copilot") or {}
    print(f"Copilot 提醒：{'已推送' if copilot.get('pushed') else '未推送'}（{copilot.get('note') or 'ok'}）")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
