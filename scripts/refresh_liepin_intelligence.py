#!/usr/bin/env python3
"""Refresh the local Liepin intelligence loop in one command."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = BASE_DIR / "scripts"
DEFAULT_OUTPUT_DIR = BASE_DIR / "outputs"
READ_ONLY_PRIVATE_VAULT = DEFAULT_OUTPUT_DIR / "_read_only_private_vault"
READ_ONLY_DAILY_ACTIONS = READ_ONLY_PRIVATE_VAULT / "60_Reviews" / "今日行动清单.md"


STEPS = [
    ("sync_samples", "同步回复助手采纳样本", ["sync_reply_assistant_samples.py"]),
    ("sync_outreach", "同步回复助手触达事件", ["sync_reply_assistant_outreach_events.py"]),
    ("backfill_talk_samples", "回填历史职聊标签", ["backfill_talk_samples_strategy.py"]),
    ("chat_history_mining", "挖掘历史职聊话术", ["mine_liepin_chat_history.py"]),
    ("conversation_context_mining", "挖掘真实对话上下文", ["mine_liepin_conversation_contexts.py"]),
    ("reply_learning", "生成话术学习器", ["generate_reply_learning_report.py"]),
    ("talk_quality_schema", "检查话术质量底座", ["ensure_talk_quality_schema.py"]),
    ("talk_drafts_initial", "生成跟进话术草稿", ["generate_talk_drafts.py"]),
    ("score_high_value_followups", "按置信度重算快推分层", ["score_high_value_followups.py"]),
    ("talk_drafts_final", "按最新分层重算话术草稿", ["generate_talk_drafts.py"]),
    ("talk_quality", "生成话术质量报告", ["generate_talk_quality_report.py"]),
    ("lane_ready_messages", "生成分层可发送话术", ["generate_lane_ready_messages.py"]),
    ("fast_lane_cards", "生成今日快推卡片", ["generate_fast_lane_cards.py"]),
    ("profile_standardization", "标准化岗位/候选人画像", ["generate_profile_standardization.py"]),
    ("candidate_intelligence", "重算候选人基础画像", ["generate_candidate_intelligence.py"]),
    ("outreach_readiness", "生成推荐前校验", ["generate_outreach_readiness.py"]),
    ("batch_recommendation_summary", "生成客户推荐汇总", ["generate_batch_recommendation_summary.py"]),
    ("wakeup_opportunities", "生成唤醒机会清单", ["generate_wakeup_opportunities.py"]),
    ("reply_dashboard", "生成回复驾驶舱", ["generate_reply_dashboard.py"]),
    ("today_board", "生成今日优先处理人选", ["generate_today_priority_board.py"]),
    ("position_dashboard", "生成岗位驾驶舱", ["generate_position_dashboard.py"]),
    ("position_detail_pages", "生成岗位详情页", ["generate_position_detail_pages.py"]),
    ("position_action_hub", "生成岗位推进入口", ["generate_position_action_hub.py"]),
    ("project_assignment_list", "生成项目归属待修正清单", ["confirm_project_assignment.py", "--list"]),
    ("client_feedback_report", "生成客户反馈闭环", ["generate_client_feedback_report.py"]),
    ("search_experiment_report", "生成搜索实验复盘", ["generate_search_experiment_report.py"]),
    ("strategy_corrections", "生成策略修正规则", ["generate_strategy_corrections.py"]),
    ("next_search_strategy", "生成下一轮搜索策略", ["generate_next_search_strategy.py"]),
    ("workflow_report", "生成主任务树推进报告", ["generate_workflow_status_report.py"]),
    ("workbench", "生成猎聘智能工作台", ["generate_liepin_workbench.py"]),
]

OPTIONAL_STEPS = [
    ("obsidian_private_vault", "同步 Obsidian 私密主库", ["sync_obsidian_private_vault.py"]),
    ("obsidian_private_vault_audit", "体检 Obsidian 私密主库", ["audit_obsidian_private_vault.py"]),
    ("followup_assignment_triage", "分流待办项目归属", ["triage_followup_assignments.py"]),
    ("confirmation_queue", "生成待确认清单", ["generate_confirmation_queue.py"]),
    ("decision_summary", "生成每日拍板摘要", ["generate_decision_summary.py"]),
    ("confirmation_manual", "生成确认后执行手册", ["generate_confirmation_execution_manual.py"]),
    ("read_only_action_package", "生成今日行动清单", ["generate_read_only_action_package.py"]),
    ("daily_ops_console", "刷新日常操作台", ["generate_daily_ops_console.py"]),
]

READ_ONLY_DB_SOURCE_STEPS = {
    "today_board",
    "position_dashboard",
    "position_action_hub",
    "outreach_readiness",
    "batch_recommendation_summary",
    "client_feedback_report",
    "search_experiment_report",
    "next_search_strategy",
    "workflow_report",
}

READ_ONLY_SUMMARY_STEPS = {
    "confirmation_queue",
    "decision_summary",
    "confirmation_manual",
    "read_only_action_package",
    "daily_ops_console",
}

PASSTHROUGH_STEPS = READ_ONLY_DB_SOURCE_STEPS | {
    "sync_samples",
    "sync_outreach",
    "backfill_talk_samples",
    "chat_history_mining",
    "conversation_context_mining",
    "reply_learning",
    "talk_quality_schema",
    "talk_drafts_initial",
    "score_high_value_followups",
    "talk_drafts_final",
    "talk_quality",
    "lane_ready_messages",
    "fast_lane_cards",
    "profile_standardization",
    "candidate_intelligence",
    "wakeup_opportunities",
    "reply_dashboard",
    "position_detail_pages",
    "project_assignment_list",
    "strategy_corrections",
    "workbench",
    "obsidian_private_vault",
    "obsidian_private_vault_audit",
    "followup_assignment_triage",
}

READ_ONLY_BLOCKED_STEPS = {
    "sync_samples",
    "sync_outreach",
    "backfill_talk_samples",
    "chat_history_mining",
    "conversation_context_mining",
    "reply_learning",
    "talk_quality_schema",
    "talk_drafts_initial",
    "score_high_value_followups",
    "talk_drafts_final",
    "talk_quality",
    "lane_ready_messages",
    "fast_lane_cards",
    "profile_standardization",
    "candidate_intelligence",
    "outreach_readiness",
    "batch_recommendation_summary",
    "wakeup_opportunities",
    "reply_dashboard",
    "today_board",
    "position_dashboard",
    "position_detail_pages",
    "position_action_hub",
    "project_assignment_list",
    "client_feedback_report",
    "search_experiment_report",
    "strategy_corrections",
    "next_search_strategy",
    "workflow_report",
    "workbench",
    "obsidian_private_vault",
    "obsidian_private_vault_audit",
    "followup_assignment_triage",
}


def run_step(name: str, label: str, command: list[str], extra_args: list[str], blocking: bool = True) -> dict:
    script = SCRIPTS_DIR / command[0]
    cmd = [sys.executable, str(script), *command[1:], *extra_args]
    started = datetime.now().isoformat(timespec="seconds")
    proc = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True)
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    parsed = None
    if stdout:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = None
    return {
        "name": name,
        "label": label,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "started": started,
        "command": cmd,
        "stdout": stdout,
        "stderr": stderr,
        "parsed": parsed,
        "blocking": blocking,
    }


def passthrough_args_for_step(name: str, passthrough: list[str]) -> list[str]:
    if name in PASSTHROUGH_STEPS:
        return list(passthrough)
    return []


def write_report(output_dir: Path, results: list[dict], stamp: str | None = None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘智能一键刷新记录_{stamp}.md"
    lines = [
        "# 猎聘智能一键刷新记录",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 执行结果",
        "",
        "| 步骤 | 状态 | 输出 |",
        "|---|---|---|",
    ]
    for result in results:
        parsed = result.get("parsed") or {}
        output = parsed.get("report") or parsed.get("receipt") or ""
        if not output and result.get("stderr"):
            output = result["stderr"].splitlines()[-1][:120]
        lines.append(
            f"| {result['label']} | {'成功' if result['ok'] else ('失败' if result.get('blocking', True) else '失败（不阻断）')} | {output or '无'} |"
        )

    lines.extend(["", "## 最新可看文件", ""])
    for result in results:
        parsed = result.get("parsed") or {}
        output = parsed.get("report") or parsed.get("receipt")
        if output:
            lines.append(f"- {result['label']}：{output}")

    failed = [result for result in results if not result["ok"] and result.get("blocking", True)]
    non_blocking_failed = [result for result in results if not result["ok"] and not result.get("blocking", True)]
    if failed:
        lines.extend(["", "## 需要处理", ""])
        for result in failed:
            lines.append(f"- {result['label']}：{result['stderr'] or result['stdout'] or '未知错误'}")
    if non_blocking_failed:
        lines.extend(["", "## 不阻断主链路的问题", ""])
        for result in non_blocking_failed:
            lines.append(f"- {result['label']}：{result['stderr'] or result['stdout'] or '未知错误'}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh Liepin intelligence outputs.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--skip-samples", action="store_true")
    parser.add_argument("--skip-outreach", action="store_true")
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument(
        "--read-only-from-db",
        action="store_true",
        help="只读重算本地 DB 驱动的源产物，再生成摘要层文件；不写库、不触达、不打开浏览器。",
    )
    args, passthrough = parser.parse_known_args()
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    results: list[dict] = []
    for name, label, command in STEPS:
        if args.read_only_from_db and name not in READ_ONLY_DB_SOURCE_STEPS:
            continue
        if args.read_only and name in READ_ONLY_BLOCKED_STEPS:
            continue
        if name == "sync_samples" and args.skip_samples:
            continue
        if name == "sync_outreach" and args.skip_outreach:
            continue
        results.append(run_step(name, label, command, passthrough_args_for_step(name, passthrough)))
        if not results[-1]["ok"]:
            break

    if all(result["ok"] or not result.get("blocking", True) for result in results):
        for name, label, command in OPTIONAL_STEPS:
            if args.read_only_from_db and name not in READ_ONLY_SUMMARY_STEPS:
                continue
            if args.read_only and name in READ_ONLY_BLOCKED_STEPS:
                continue
            step_args = passthrough_args_for_step(name, passthrough)
            if args.read_only and name in {"decision_summary", "confirmation_manual", "daily_ops_console", "confirmation_queue"}:
                step_args.extend(["--private-vault", str(READ_ONLY_PRIVATE_VAULT), "--stamp", run_stamp])
            if args.read_only and name == "read_only_action_package":
                step_args.extend(["--private-vault", str(READ_ONLY_PRIVATE_VAULT), "--stamp", run_stamp])
            if args.read_only and name == "daily_ops_console":
                step_args.extend(["--daily-actions", str(READ_ONLY_DAILY_ACTIONS)])
            if args.read_only_from_db and name in {
                "confirmation_queue",
                "decision_summary",
                "confirmation_manual",
                "read_only_action_package",
                "daily_ops_console",
            }:
                step_args.extend(["--private-vault", str(READ_ONLY_PRIVATE_VAULT), "--stamp", run_stamp])
            if args.read_only_from_db and name == "daily_ops_console":
                step_args.extend(["--daily-actions", str(READ_ONLY_DAILY_ACTIONS)])
            results.append(run_step(name, label, command, step_args, blocking=False))

    report = write_report(Path(args.output_dir).expanduser(), results, stamp=run_stamp)
    blocking_ok = all(result["ok"] for result in results if result.get("blocking", True))
    print(
        json.dumps(
            {
                "ok": blocking_ok,
                "steps": [
                    {
                        "name": result["name"],
                        "ok": result["ok"],
                        "blocking": result.get("blocking", True),
                        "report": (result.get("parsed") or {}).get("report"),
                        "receipt": (result.get("parsed") or {}).get("receipt"),
                    }
                    for result in results
                ],
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if blocking_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
