#!/usr/bin/env python3
"""Generate the daily operations console for the Liepin intelligence project."""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = BASE_DIR / "outputs"
DEFAULT_PRIVATE_VAULT = Path.home() / "Documents" / "Obsidian Liepin Private Vault"
DEFAULT_DAILY_ACTIONS = DEFAULT_PRIVATE_VAULT / "60_Reviews" / "今日行动清单.md"


def latest_file(output_dir: Path, pattern: str) -> Path | None:
    files = sorted(output_dir.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)
    return files[0] if files else None


def stamped_file(output_dir: Path, prefix: str, stamp: str | None) -> Path | None:
    if not stamp:
        return None
    path = output_dir / f"{prefix}_{stamp}.md"
    return path if path.exists() else None


def stamped_path(output_dir: Path, prefix: str, stamp: str | None) -> Path | None:
    if not stamp:
        return None
    return output_dir / f"{prefix}_{stamp}.md"


def abs_link(path: Path | None, label: str) -> str:
    if path is None:
        return f"- {label}（未生成）"
    target = path.expanduser().resolve()
    return f"[{label}](<{target}>)"


def read_text(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def extract_section(text: str, heading: str) -> str:
    if not text:
        return ""
    pattern = rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)"
    match = re.search(pattern, text, flags=re.S | re.M)
    return match.group(1).strip() if match else ""


def table_rows(block: str, limit: int = 3) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in block.splitlines():
        if not line.startswith("| "):
            continue
        if set(line.replace("|", "").strip()) <= {"-", ":"}:
            continue
        cells = [cell.strip().replace("｜", "|") for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        rows.append(cells)
        if len(rows) >= limit:
            break
    return rows


def parse_summary(text: str) -> dict[str, str]:
    summary = {
        "待处理画像": "暂无",
        "A/B 优先推进": "暂无",
        "需补客户/岗位": "暂无",
        "需转联系方式或谈薪": "暂无",
        "快推池": "暂无",
        "轻跟进池": "暂无",
        "暂缓/沉淀": "暂无",
    }
    for key in summary:
        match = re.search(rf"- {re.escape(key)}：([^\n]+)", text)
        if match:
            summary[key] = match.group(1).strip()
    return summary


def parse_priority_tables(text: str) -> dict[str, list[list[str]]]:
    return {
        "先推进": table_rows(extract_section(text, "先推进"), limit=3),
        "先补确认": table_rows(extract_section(text, "先补确认"), limit=3),
        "转微信/薪资处理": table_rows(extract_section(text, "转微信/薪资处理"), limit=3),
        "轻跟进池": table_rows(extract_section(text, "轻跟进池"), limit=3),
    }


def parse_thin_jobs(text: str, limit: int = 5) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in text.splitlines():
        if not line.startswith("| 补搜索 |") and not line.startswith("| 补归属 |"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 6:
            rows.append(cells)
        if len(rows) >= limit:
            break
    return rows


def build_console(output_dir: Path, private_vault: Path, daily_actions: Path, stamp: str | None = None) -> str:
    today_board = latest_file(output_dir, "猎聘今日优先处理人选_*.md")
    decision_summary = stamped_file(output_dir, "猎聘每日拍板摘要", stamp) or latest_file(output_dir, "猎聘每日拍板摘要_*.md")
    confirmation_manual = stamped_file(output_dir, "猎聘确认后执行手册", stamp) or latest_file(output_dir, "猎聘确认后执行手册_*.md")
    position_hub = latest_file(output_dir, "猎聘岗位推进入口_*.md")
    recommendation = latest_file(output_dir, "猎聘客户推荐汇总_*.md")
    refresh_report = stamped_path(output_dir, "猎聘智能一键刷新记录", stamp) or latest_file(output_dir, "猎聘智能一键刷新记录_*.md")
    vault_audit = latest_file(output_dir, "猎聘私密主库数据质量体检_*.md")
    triage_report = latest_file(output_dir, "猎聘待办归属分流_*.md")
    fast_lane = latest_file(output_dir, "猎聘今日7条快推卡片_*.md")
    workflow = BASE_DIR / "猎聘全流程智能化任务树.md"

    today_text = read_text(today_board)
    hub_text = read_text(position_hub)
    summary = parse_summary(today_text)
    tables = parse_priority_tables(today_text)
    thin_jobs = parse_thin_jobs(hub_text)

    lines = [
        "# 猎聘日常操作台",
        "",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "这个页面只放日常执行入口，不放长报告。",
        "",
        "## 今天先做什么",
        "",
        "1. 先看 `每日拍板摘要`，把今天最需要点头的 3-5 件事先定下来。",
        "2. 再看 `确认后执行手册`，确认每类事项对应跑哪个脚本。",
        "3. 再看 `今日行动清单`，把 10 条快推、8 个 P1 补信息和薄岗补搜按顺序过一遍。",
        "4. 再看 `今日优先处理人选`，把今天要先推的人选挑出来。",
        "5. 再看 `岗位推进入口`，确认哪些岗位先补信息，哪些岗位先补搜索。",
        "6. 有明确回复的人选，先补一个关键缺口，再决定是否推进。",
        "7. 只有在客户、岗位、硬性门槛都明确后，才考虑真实外发触达。",
        "8. 薄岗先补搜索，不要一上来硬推。",
        "",
        "## 今日边界",
        "",
        f"- 待处理画像：{summary['待处理画像']}",
        f"- A/B 优先推进：{summary['A/B 优先推进']}",
        f"- 需补客户/岗位：{summary['需补客户/岗位']}",
        f"- 需转联系方式或谈薪：{summary['需转联系方式或谈薪']}",
        f"- 快推池：{summary['快推池']}",
        f"- 轻跟进池：{summary['轻跟进池']}",
        f"- 暂缓/沉淀：{summary['暂缓/沉淀']}",
        "",
        "## 快推 / 补信息 / 需确认",
        "",
        "### 先推进",
    ]
    if tables["先推进"]:
        for cells in tables["先推进"]:
            lines.append(f"- {cells[1]}｜{cells[2]}｜{cells[4]}｜{cells[5]}")
    else:
        lines.append("- 暂无")

    lines.extend(
        [
            "",
            "### 先补确认",
        ]
    )
    if tables["先补确认"]:
        for cells in tables["先补确认"]:
            lines.append(f"- {cells[1]}｜{cells[2]}｜{cells[4]}｜{cells[5]}")
    else:
        lines.append("- 暂无")

    lines.extend(
        [
            "",
            "### 转微信/薪资处理",
        ]
    )
    if tables["转微信/薪资处理"]:
        for cells in tables["转微信/薪资处理"]:
            lines.append(f"- {cells[1]}｜{cells[2]}｜{cells[4]}｜{cells[5]}")
    else:
        lines.append("- 暂无")

    lines.extend(
        [
            "",
            "## 薄岗提醒",
            "",
        ]
    )
    if thin_jobs:
        for cells in thin_jobs:
            lines.append(f"- {cells[0]}｜{cells[2]}｜{cells[4]}｜{cells[5]}")
    else:
        lines.append("- 暂无补搜索提醒")

    lines.extend(
        [
            "",
            "## 页面入口",
            "",
            f"- {abs_link(decision_summary, '每日拍板摘要')}",
            f"- {abs_link(confirmation_manual, '确认后执行手册')}",
            f"- {abs_link(daily_actions, '今日行动清单')}",
            f"- {abs_link(today_board, '今日优先处理人选')}",
            f"- {abs_link(position_hub, '岗位推进入口')}",
            f"- {abs_link(recommendation, '客户推荐汇总')}",
            f"- {abs_link(refresh_report, '一键刷新记录')}",
            f"- {abs_link(vault_audit, '私密主库体检')}",
            f"- {abs_link(triage_report, '待办归属分流')}",
            f"- {abs_link(fast_lane, '今日 7 条快推卡片')}",
            f"- {abs_link(workflow, '项目主任务树')}",
            "",
            "## 对应脚本和按钮",
            "",
            f"- {abs_link(BASE_DIR / 'scripts' / 'refresh_liepin_intelligence.py', '一键刷新脚本')}",
            f"- {abs_link(BASE_DIR / 'scripts' / 'generate_daily_ops_console.py', '日常操作台脚本')}",
            f"- {abs_link(BASE_DIR / 'scripts' / 'generate_decision_summary.py', '每日拍板摘要脚本')}",
            f"- {abs_link(BASE_DIR / 'scripts' / 'generate_confirmation_execution_manual.py', '确认后执行手册脚本')}",
            f"- {abs_link(BASE_DIR / 'scripts' / 'generate_today_priority_board.py', '今日优先处理脚本')}",
            f"- {abs_link(BASE_DIR / 'scripts' / 'generate_position_action_hub.py', '岗位推进入口脚本')}",
            f"- {abs_link(BASE_DIR / 'scripts' / 'generate_workflow_status_report.py', '主任务树报告脚本')}",
            f"- {abs_link(BASE_DIR / 'scripts' / 'sync_obsidian_private_vault.py', '私密库同步脚本')}",
            f"- {abs_link(BASE_DIR / 'scripts' / 'triage_followup_assignments.py', '待办归属分流脚本')}",
            f"- {abs_link(BASE_DIR / 'scripts' / 'confirm_project_assignment.py', '修正项目归属脚本')}",
            f"- {abs_link(BASE_DIR / '刷新猎聘智能.command', '刷新猎聘智能按钮')}",
            f"- {abs_link(BASE_DIR / '刷新日常操作台.command', '刷新日常操作台按钮')}",
            f"- {abs_link(BASE_DIR / '打开猎聘智能工作台.command', '打开工作台按钮')}",
            f"- {abs_link(BASE_DIR / '重启猎聘工作台服务.command', '重启工作台按钮')}",
            f"- {abs_link(BASE_DIR / '修正猎聘项目归属.command', '修正归属按钮')}",
            f"- {abs_link(BASE_DIR / '记录客户反馈.command', '记录客户反馈按钮')}",
            f"- {abs_link(BASE_DIR / '记录猎聘搜索实验.command', '记录搜索实验按钮')}",
            "",
            "## 风险提醒",
            "",
            "- 不能直接外发触达，必须先确认客户、岗位和硬性门槛。",
            "- 没有项目归属或只有泛方向时，先补信息，不要硬推。",
            "- 薄岗先补搜索，再决定要不要发。",
            "- 私密库可以存实名结构化信息，但不要把联系方式全文、聊天全文、简历全文写进公开知识库。",
            "- API key、token、Cookie、账号密码、代理订阅 URL 永远不要进任何 Obsidian 库。",
            "",
            "## 需要用户确认后才能真实发送的动作",
            "",
            "- 真正发猎聘 IM 快推",
            "- 真正转微信",
            "- 真正把待办改成已发送或已确认",
            "- 真正把候选人归到某个客户/岗位",
            "- 真正把客户反馈写成对外可见的结论",
            "- 真正把任何带敏感信息的内容同步到公开知识库",
            "",
            "## 检查清单",
            "",
            "- [ ] 今日拍板摘要已先看完",
            "- [ ] 确认后执行手册已对照过",
            "- [ ] 今日行动清单已打开",
            "- [ ] 10 条快推已分成可发 / 需补 / 需缓",
            "- [ ] 8 个 P1 已补上一个关键缺口",
            "- [ ] 薄岗是否补搜已经确认",
            "- [ ] 任何外发前都已确认客户、岗位、硬性门槛",
            "- [ ] 刷新后已看一遍一键刷新记录",
            "- [ ] 私密库同步日志没有异常",
            "",
            "## 操作顺序",
            "",
            "1. 打开 `今日行动清单`",
            "2. 打开 `今日优先处理人选`",
            "3. 打开 `岗位推进入口`",
            "4. 把能发的先分出来，但先不要点发送",
            "5. 对需要确认的候选人只补一个关键问题",
            "6. 完成后跑一键刷新",
        ]
    )
    return "\n".join(lines) + "\n"


def write_outputs(console: str, private_vault: Path) -> dict[str, str]:
    console_path = BASE_DIR / "猎聘日常操作台.md"
    private_copy = private_vault / "60_Reviews" / "日常操作台.md"
    console_path.write_text(console, encoding="utf-8")
    private_copy.write_text(console, encoding="utf-8")
    return {"console": str(console_path), "private_copy": str(private_copy)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the daily operations console.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--private-vault", default=str(DEFAULT_PRIVATE_VAULT))
    parser.add_argument("--daily-actions", default=str(DEFAULT_DAILY_ACTIONS))
    parser.add_argument("--stamp")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser()
    private_vault = Path(args.private_vault).expanduser()
    daily_actions = Path(args.daily_actions).expanduser()
    console = build_console(output_dir, private_vault, daily_actions, stamp=args.stamp)
    result = write_outputs(console, private_vault)
    print(
        {
            "ok": True,
            "console": result["console"],
            "private_copy": result["private_copy"],
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
