#!/usr/bin/env python3
"""Generate the confirmation queue for actions that need human approval."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = BASE_DIR / "outputs"
DEFAULT_PRIVATE_VAULT = Path.home() / "Documents" / "Obsidian Liepin Private Vault"


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


def parse_pipeline_rows(lines: list[str], heading: str, limit: int | None = None) -> list[list[str]]:
    block = pick_section(lines, heading)
    rows: list[list[str]] = []
    in_table = False
    for line in block:
        if not line.startswith("|"):
            if in_table:
                break
            continue
        in_table = True
        clean = line.replace("|", "").strip()
        if set(clean) <= {"-", ":"} or clean.startswith("分数"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 6:
            rows.append(cells)
            if limit and len(rows) >= limit:
                break
    return rows


def parse_confirm_rows(lines: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    in_table = False
    for line in lines:
        if line.startswith("## 先补确认"):
            in_table = False
            continue
        if line.startswith("| 分数 | 候选人 | 项目 | 意图 | 下一步 | 原话 |"):
            in_table = True
            continue
        if not in_table:
            continue
        if not line.startswith("|"):
            break
        clean = line.replace("|", "").strip()
        if set(clean) <= {"-", ":"} or clean.startswith("状态"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 6:
            rows.append(cells)
    return rows


def parse_recommendation_sections(lines: list[str]) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    current = None
    buf: list[str] = []
    for line in lines:
        if line.startswith("### "):
            if current is not None:
                sections.append((current, buf))
            current = line[4:].strip()
            buf = [line]
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections.append((current, buf))
    return sections


def urgency_rank(kind: str, score: int, project: str, note: str) -> tuple[int, int]:
    if "真正发猎聘 IM 快推" in note or "可直接发" in note:
        return (0, -score)
    if "真正转微信" in note or "可转微信" in note:
        return (0, -score)
    if "待办改成已发送" in note or "已确认" in note:
        return (0, -score)
    if "补公司/岗位关键信息" in note:
        return (1, -score)
    if "补一轮硬性门槛确认" in note:
        return (2, -score)
    if "先补问" in note:
        return (3, -score)
    return (4, -score)


def safe_score(value: str) -> int | None:
    text = (value or "").strip()
    if not text or text in {"-", "未填"}:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def build_queue(output_dir: Path, stamp: str | None = None) -> str:
    today_path = latest_file(output_dir, "猎聘今日优先处理人选_*.md")
    rec_path = latest_file(output_dir, "猎聘客户推荐汇总_*.md")
    ready_path = latest_file(output_dir, "猎聘分层可直接发送话术_*.md")
    out_path = latest_file(output_dir, "猎聘推荐前校验_*.md")
    refresh_path = stamped_path(output_dir, "猎聘智能一键刷新记录", stamp) or latest_file(output_dir, "猎聘智能一键刷新记录_*.md")
    ops_path = BASE_DIR / "猎聘日常操作台.md"

    today_lines = load_lines(today_path)
    rec_lines = load_lines(rec_path)
    out_lines = load_lines(out_path)

    queue: list[dict[str, str | int]] = []

    for row in parse_pipeline_rows(today_lines, "## 先推进", limit=20):
        score = safe_score(row[0])
        if score is None:
            continue
        queue.append(
            {
                "priority": 0,
                "score": score,
                "label": row[1],
                "project": row[2],
                "action": row[4],
                "source": f"{link(today_path, '今日优先处理人选')}#先推进",
                "ops": link(ops_path, "日常操作台"),
            }
        )

    for row in parse_pipeline_rows(today_lines, "## 先补确认", limit=20):
        score = safe_score(row[0])
        if score is None:
            continue
        queue.append(
            {
                "priority": 1,
                "score": score,
                "label": row[1],
                "project": row[2],
                "action": row[4],
                "source": f"{link(today_path, '今日优先处理人选')}#先补确认",
                "ops": link(ops_path, "日常操作台"),
            }
        )

    for row in parse_confirm_rows(out_lines)[:20]:
        score = safe_score(row[0])
        if score is None:
            continue
        queue.append(
            {
                "priority": 1,
                "score": score,
                "label": row[1],
                "project": row[2],
                "action": row[4],
                "source": f"{link(out_path, '推荐前校验')}#可推进清单",
                "ops": link(ops_path, "日常操作台"),
            }
        )

    for row in parse_pipeline_rows(today_lines, "## 转微信/薪资处理", limit=10):
        score = safe_score(row[0])
        if score is None or row[1] == "暂无":
            continue
        queue.append(
            {
                "priority": 0,
                "score": score,
                "label": row[1],
                "project": row[2],
                "action": row[4],
                "source": f"{link(today_path, '今日优先处理人选')}#转微信",
                "ops": link(ops_path, "日常操作台"),
            }
        )

    for section, buf in parse_recommendation_sections(rec_lines):
        if not section.startswith("### "):
            continue
        project = section.replace("### ", "").strip()
        for line in buf:
            if line.startswith("| 最推荐 |") or line.startswith("| 备选 |") or line.startswith("| 风险高 |"):
                cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
                if len(cells) < 7:
                    continue
                if cells[1] == "分" or cells[2] == "人选":
                    continue
                score = safe_score(cells[1])
                if score is None:
                    continue
                note = cells[6]
                if "补公司/岗位关键信息" in note or "补一轮硬性门槛确认" in note or "先补问" in note:
                    queue.append(
                        {
                            "priority": 2 if "补公司/岗位关键信息" in note else 3,
                            "score": score,
                            "label": cells[2],
                            "project": project,
                            "action": note,
                            "source": f"{link(rec_path, '客户推荐汇总')}#{project}",
                            "ops": link(ops_path, "日常操作台"),
                        }
                    )

    dedup: dict[tuple[str, str, str], dict[str, str | int]] = {}
    for item in queue:
        key = (str(item["label"]), str(item["project"]), str(item["action"]))
        prev = dedup.get(key)
        if prev is None or urgency_rank("x", int(item["score"]), str(item["project"]), str(item["action"])) < urgency_rank("x", int(prev["score"]), str(prev["project"]), str(prev["action"])):
            dedup[key] = item

    ordered = sorted(
        dedup.values(),
        key=lambda item: (
            item["priority"],
            -int(item["score"]),
            str(item["project"]),
            str(item["label"]),
        ),
    )

    lines = [
        "# 猎聘待确认清单",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "这是所有需要人工确认后才能真实发送、真实触达、真实写回的动作汇总。",
        "",
        "## 排序规则",
        "",
        "1. 先排会直接产生外发或状态写回的动作。",
        "2. 再排补信息动作，优先补公司/岗位锚点。",
        "3. 最后排补硬性门槛确认和轻量追问。",
        "",
        "## 待确认队列",
        "",
        "| 紧急度 | 分数 | 候选人 | 项目 | 动作 | 来源 | 回链 |",
        "|---|---:|---|---|---|---|---|",
    ]
    for item in ordered[:60]:
        urgency = "P0" if int(item["priority"]) == 0 else ("P1" if int(item["priority"]) == 1 else ("P2" if int(item["priority"]) == 2 else "P3"))
        lines.append(
            f"| {urgency} | {item['score']} | {str(item['label']).replace('|', '｜')} | {str(item['project']).replace('|', '｜')} | {str(item['action']).replace('|', '｜')} | {str(item['source']).replace('|', '｜')} | {str(item['ops']).replace('|', '｜')} |"
        )

    lines.extend(
        [
            "",
            "## 入口",
            "",
            f"- {link(ops_path, '日常操作台')}",
            f"- {link(refresh_path, '一键刷新记录')}",
            f"- {link(today_path, '今日优先处理人选')}",
            f"- {link(rec_path, '客户推荐汇总')}",
            f"- {link(ready_path, '分层可直接发送话术')}",
            f"- {link(out_path, '推荐前校验')}",
            "",
            "## 说明",
            "",
            "- 清单里出现的动作只表示“需要你确认”，不代表已经可以发送。",
            "- 任何真实触达、微信转发、待办改写、候选人归属写入，都必须从这份清单里先确认。",
        ]
    )
    return "\n".join(lines) + "\n"


def write_outputs(text: str, private_vault: Path, stamp: str | None = None) -> dict[str, str]:
    output_dir = BASE_DIR / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    private_dir = private_vault / "60_Reviews"
    private_dir.mkdir(parents=True, exist_ok=True)
    primary = private_dir / "待确认清单.md"
    secondary = output_dir / f"猎聘待确认清单_{stamp}.md"
    primary.write_text(text, encoding="utf-8")
    secondary.write_text(text, encoding="utf-8")
    return {"private": str(primary), "report": str(secondary)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate confirmation queue for approval-only actions.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--private-vault", default=str(DEFAULT_PRIVATE_VAULT))
    parser.add_argument("--stamp")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser()
    private_vault = Path(args.private_vault).expanduser()
    text = build_queue(output_dir, stamp=args.stamp)
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
