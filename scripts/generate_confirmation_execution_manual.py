#!/usr/bin/env python3
"""Generate the confirmation-after-execution manual for Liepin actions."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = BASE_DIR / "outputs"
DEFAULT_PRIVATE_VAULT = Path.home() / "Documents" / "Obsidian Liepin Private Vault"
DEFAULT_OPS_CONSOLE = BASE_DIR / "猎聘日常操作台.md"


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


def link(path: Path | None, label: str) -> str:
    if path is None:
        return "未生成"
    return f"[{label}](<{path.expanduser().resolve()}>)"


def manual_rows() -> list[dict[str, str]]:
    return [
        {
            "category": "真实发 IM / 快推",
            "summary": "对已确认可发的人选，执行真实猎聘 IM 快推或进入可发话术层。",
            "script": "先看 `每日拍板摘要` -> `待确认清单`；确认后通常继续看 `分层可直接发送话术`，必要时回到 `今日优先处理人选` 复核。",
            "files": "会产出 `猎聘分层可直接发送话术_*.md`、`猎聘今日优先处理人选_*.md`、`猎聘智能一键刷新记录_*.md`。",
            "second": "发出前仍需确认客户、岗位、硬性门槛和候选人是否真的适合进入真实触达。",
        },
        {
            "category": "转微信 / 私聊推进",
            "summary": "对可转微信的人选，先确认微信边界，再进入真实转发或私聊推进。",
            "script": "先看 `每日拍板摘要` -> `待确认清单`；确认后再走真实触达流程，必要时参考 `推荐前校验` 和 `今日优先处理人选`。",
            "files": "会产出 `猎聘待确认清单_*.md`、`猎聘推荐前校验_*.md`、`猎聘智能一键刷新记录_*.md`。",
            "second": "转微信前仍要确认地点、薪资、求职意愿等关键项，避免直接外发后再补救。",
        },
        {
            "category": "补公司 / 岗位关键信息",
            "summary": "对信息不完整的人选，先补一个关键问题，再决定是否推进。",
            "script": "先看 `每日拍板摘要` -> `待确认清单`；确认后可继续参考 `今日优先处理人选` 和 `推荐前校验`。",
            "files": "会产出 `猎聘待确认清单_*.md`、`猎聘今日优先处理人选_*.md`、`猎聘推荐前校验_*.md`。",
            "second": "只补一个问题，不要顺手扩成整段追问；补完后仍要二次看是否满足推进条件。",
        },
        {
            "category": "硬性门槛确认",
            "summary": "对分数够但门槛未实锤的人选，先补硬性信息，再决定是否进入推荐前校验。",
            "script": "先看 `每日拍板摘要` -> `待确认清单`；确认后继续看 `推荐前校验` 和 `客户推荐汇总`。",
            "files": "会产出 `猎聘推荐前校验_*.md`、`猎聘客户推荐汇总_*.md`、`猎聘待确认清单_*.md`。",
            "second": "确认后也只是进入可推进层，不等于可以立刻发出或改写最终状态。",
        },
        {
            "category": "项目归属确认 / 修正写回",
            "summary": "对客户、岗位归属不清的人选，执行项目归属修正。",
            "script": "先看 `待办归属分流` 或 `项目归属待修正清单`；确认后再运行 `confirm_project_assignment.py` 写回。",
            "files": "会产出 `猎聘项目归属修正_list_*.md`、`猎聘待办归属分流_*.md`、`猎聘智能一键刷新记录_*.md`。",
            "second": "这是会改数据库的动作，必须再确认一次客户名和岗位名，避免写错主键归属。",
        },
        {
            "category": "客户反馈写入",
            "summary": "对客户评价、面试结果、offer、拒绝等反馈做结构化记录。",
            "script": "先看 `每日拍板摘要` 或相关反馈来源；确认后再跑 `record_client_feedback.py`。",
            "files": "会产出 `猎聘客户反馈记录_*.md`、`猎聘客户反馈闭环_*.md`、`猎聘智能一键刷新记录_*.md`。",
            "second": "写入反馈前要确认候选人、项目和反馈类型，且不要把原话全文塞进公开知识库。",
        },
    ]


def build_manual(output_dir: Path, private_vault: Path, stamp: str | None = None) -> str:
    summary_path = stamped_file(output_dir, "猎聘每日拍板摘要", stamp) or latest_file(output_dir, "猎聘每日拍板摘要_*.md")
    waitlist_path = stamped_file(output_dir, "猎聘待确认清单", stamp) or latest_file(output_dir, "猎聘待确认清单_*.md")
    ops_path = DEFAULT_OPS_CONSOLE
    refresh_path = stamped_path(output_dir, "猎聘智能一键刷新记录", stamp) or latest_file(output_dir, "猎聘智能一键刷新记录_*.md")

    lines = [
        "# 猎聘确认后执行手册",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "这份手册只说明“确认后怎么执行”，不替你执行任何真实外发或不可逆写回。",
        "",
        "## 总入口",
        "",
        f"- {link(ops_path, '日常操作台')}",
        f"- {link(summary_path, '每日拍板摘要')}",
        f"- {link(waitlist_path, '待确认清单')}",
        f"- {link(refresh_path, '一键刷新记录')}",
        "",
        "## 确认后执行路径",
        "",
        "| 类别 | 确认后该跑什么 | 会产出什么文件 | 仍需二次确认什么 |",
        "|---|---|---|---|",
    ]
    for row in manual_rows():
        lines.append(
            f"| {row['category']} | {row['script']} | {row['files']} | {row['second']} |"
        )

    lines.extend(
        [
            "",
            "## 统一边界",
            "",
            "- 确认前只看摘要、清单和来源，不直接触发真实发送。",
            "- 进入执行脚本后，仍要确认客户、岗位、候选人和状态是否一致。",
            "- 会写库的动作优先经过手工复核，避免把不确定归属写成最终状态。",
            "- 私密库保存结构化结果，公开知识库只接脱敏总结。",
            "",
            "## 私密库入口",
            "",
            f"- {link(private_vault / '60_Reviews' / '每日拍板摘要.md', '每日拍板摘要')}",
            f"- {link(private_vault / '60_Reviews' / '待确认清单.md', '待确认清单')}",
            f"- {link(private_vault / '60_Reviews' / '日常操作台.md', '日常操作台')}",
        ]
    )
    return "\n".join(lines) + "\n"


def write_outputs(text: str, private_vault: Path, stamp: str | None = None) -> dict[str, str]:
    output_dir = BASE_DIR / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    private_dir = private_vault / "60_Reviews"
    private_dir.mkdir(parents=True, exist_ok=True)
    primary = private_dir / "确认后执行手册.md"
    secondary = output_dir / f"猎聘确认后执行手册_{stamp}.md"
    primary.write_text(text, encoding="utf-8")
    secondary.write_text(text, encoding="utf-8")
    return {"private": str(primary), "report": str(secondary)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the confirmation-after-execution manual.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--private-vault", default=str(DEFAULT_PRIVATE_VAULT))
    parser.add_argument("--stamp")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser()
    private_vault = Path(args.private_vault).expanduser()
    text = build_manual(output_dir, private_vault, stamp=args.stamp)
    result = write_outputs(text, private_vault, stamp=args.stamp)
    print(
        {
            "ok": True,
            "private": result["private"],
            "report": result["report"],
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
