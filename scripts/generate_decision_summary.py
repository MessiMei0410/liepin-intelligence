#!/usr/bin/env python3
"""Generate the daily decision summary from the confirmation queue."""

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


def load_lines(path: Path | None) -> list[str]:
    if path is None or not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def pick_section(lines: list[str], heading: str) -> list[str]:
    start = None
    for idx, line in enumerate(lines):
        if line.strip() == heading:
            start = idx + 1
            break
    if start is None:
        return []
    result: list[str] = []
    for line in lines[start:]:
        if line.startswith("## ") and result:
            break
        if line.startswith("## ") and not result:
            continue
        result.append(line)
    return result


def parse_queue_rows(lines: list[str]) -> list[dict[str, str]]:
    block = pick_section(lines, "## 待确认队列")
    rows: list[dict[str, str]] = []
    in_table = False
    for line in block:
        if not line.startswith("|"):
            if in_table:
                break
            continue
        in_table = True
        clean = line.replace("|", "").strip()
        if set(clean) <= {"-", ":"}:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 7 or cells[0] == "紧急度":
            continue
        rows.append(
            {
                "urgency": cells[0],
                "score": cells[1],
                "candidate": cells[2],
                "project": cells[3],
                "action": cells[4],
                "source": cells[5],
                "ops": cells[6],
            }
        )
    return rows


def safe_score(value: str) -> int:
    text = (value or "").strip()
    try:
        return int(float(text))
    except ValueError:
        return 0


def action_rank(action: str) -> int:
    text = action or ""
    if "转微信" in text or "发起沟通" in text or "真正发" in text or "真实触达" in text:
        return 0
    if "补公司/岗位关键信息" in text or "补公司" in text or "补岗位" in text:
        return 1
    if "硬性门槛确认" in text or "完整简历复核" in text or "先确认硬性门槛" in text:
        return 2
    return 3


def boundary_text(action: str) -> str:
    text = action or ""
    if "转微信" in text or "发起沟通" in text or "真正发" in text or "真实触达" in text:
        return "确认后才可真实发猎聘 IM 或转微信；未确认前只保留草稿，不改写已发送状态。"
    if "补公司/岗位关键信息" in text or "补公司" in text or "补岗位" in text:
        return "确认后才可向候选人补问公司/岗位关键信息；未确认前只记为待核实，不外发。"
    if "硬性门槛确认" in text or "完整简历复核" in text or "先确认硬性门槛" in text:
        return "确认后才可进入推荐前校验和后续触达；未确认前不要推进到可发送层。"
    return "确认后才可执行；未确认前只保留为待办，不做真实发送或写回。"


def build_summary(output_dir: Path, private_vault: Path, stamp: str | None = None) -> str:
    waitlist_path = stamped_file(output_dir, "猎聘待确认清单", stamp) or latest_file(output_dir, "猎聘待确认清单_*.md")
    waitlist_lines = load_lines(waitlist_path) or load_lines(private_vault / "60_Reviews" / "待确认清单.md")
    ops_path = DEFAULT_OPS_CONSOLE
    refresh_path = stamped_path(output_dir, "猎聘智能一键刷新记录", stamp) or latest_file(output_dir, "猎聘智能一键刷新记录_*.md")

    items = parse_queue_rows(waitlist_lines)
    seen: set[tuple[str, str, str]] = set()
    ordered: list[dict[str, str]] = []
    for item in sorted(items, key=lambda row: (action_rank(row["action"]), -safe_score(row["score"]), row["project"], row["candidate"])):
        key = (item["candidate"], item["project"], item["action"])
        if key in seen:
            continue
        seen.add(key)
        ordered.append(item)
        if len(ordered) >= 5:
            break

    lines = [
        "# 猎聘每日拍板摘要",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "只列今天最需要你拍板的事项。未确认前，不做真实发送、不做真实触达、不做不可逆写回。",
        "",
        "## 今日最该拍板的 5 项",
        "",
        "| 顺位 | 紧急度 | 候选人 | 项目 | 建议动作 | 确认后才可执行的边界 | 来源 | 回链 |",
        "|---|---|---|---|---|---|---|---|",
    ]

    if not ordered:
        lines.append(f"| 1 | - | 暂无 | 暂无 | 当前没有需要拍板的事项 | 仅保留草稿，不做真实发送或写回 | {link(waitlist_path, '待确认清单')} | {link(ops_path, '日常操作台')} |")
    else:
        for idx, item in enumerate(ordered, start=1):
            lines.append(
                f"| {idx} | {item['urgency']} | {item['candidate'].replace('|', '｜')} | {item['project'].replace('|', '｜')} | {item['action'].replace('|', '｜')} | {boundary_text(item['action']).replace('|', '｜')} | {item['source']} | {link(ops_path, '日常操作台')} / {link(waitlist_path, '待确认清单')} |"
            )

    lines.extend(
        [
            "",
            "## 使用方式",
            "",
            "- 先看这一页，再进 `待确认清单` 找对应来源。",
            "- 只有你明确确认后，才把动作推进到真实发送或真实写回。",
            "",
            "## 入口",
            "",
            f"- {link(ops_path, '日常操作台')}",
            f"- {link(waitlist_path, '待确认清单')}",
            f"- {link(refresh_path, '一键刷新记录')}",
        ]
    )
    return "\n".join(lines) + "\n"


def write_outputs(text: str, private_vault: Path, stamp: str | None = None) -> dict[str, str]:
    output_dir = BASE_DIR / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    private_dir = private_vault / "60_Reviews"
    private_dir.mkdir(parents=True, exist_ok=True)
    primary = private_dir / "每日拍板摘要.md"
    secondary = output_dir / f"猎聘每日拍板摘要_{stamp}.md"
    primary.write_text(text, encoding="utf-8")
    secondary.write_text(text, encoding="utf-8")
    return {"private": str(primary), "report": str(secondary)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the daily decision summary.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--private-vault", default=str(DEFAULT_PRIVATE_VAULT))
    parser.add_argument("--stamp")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser()
    private_vault = Path(args.private_vault).expanduser()
    text = build_summary(output_dir, private_vault, stamp=args.stamp)
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
