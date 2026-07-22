#!/usr/bin/env python3
"""Run the local Liepin reply intelligence pipeline."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = ROOT / "outputs"
PNX_BUILD_SCRIPT = Path.home() / "Desktop" / "pnx_app" / "scripts" / "build.sh"


def run_step(name: str, command: list[str], cwd: Path | None = None) -> dict:
    started = datetime.now()
    proc = subprocess.run(
        command,
        cwd=str(cwd or ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    ended = datetime.now()
    payload = {
        "name": name,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "seconds": round((ended - started).total_seconds(), 2),
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }
    return payload


def parse_json_tail(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    best: dict | None = None
    best_size = -1
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and end > best_size:
            best = value
            best_size = end
    return best or {}


def summarize_step(step: dict) -> dict:
    parsed = parse_json_tail(step["stdout"])
    summary = {"name": step["name"], "ok": step["ok"], "seconds": step["seconds"]}
    for key in (
        "report",
        "markdown",
        "visible_conversations",
        "candidate_replies",
        "self_or_system",
        "ingest_summary",
    ):
        if key in parsed:
            summary[key] = parsed[key]
    return summary


def write_run_report(steps: list[dict], output_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘回复智能流水线运行记录_{stamp}.md"
    lines = [
        "# 猎聘回复智能流水线运行记录",
        "",
        f"运行时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 结果",
        "",
    ]
    for step in steps:
        status = "成功" if step["ok"] else "失败"
        lines.append(f"- {step['name']}：{status}，耗时 {step['seconds']} 秒")
        parsed = parse_json_tail(step["stdout"])
        report = parsed.get("report") or parsed.get("markdown")
        if report:
            lines.append(f"  - 输出：{report}")
        if "visible_conversations" in parsed:
            lines.append(f"  - 可见会话：{parsed.get('visible_conversations', 0)}")
            lines.append(f"  - 候选人回复：{parsed.get('candidate_replies', 0)}")
        ingest_summary = parsed.get("ingest_summary")
        if isinstance(ingest_summary, dict):
            lines.append(f"  - 新增候选人回复：{ingest_summary.get('inserted_replies', 0)}")
            lines.append(f"  - 新增跟进任务：{ingest_summary.get('inserted_tasks', 0)}")
            lines.append(f"  - 未匹配人才库候选人：{ingest_summary.get('unmatched_candidates', 0)}")
        if not step["ok"] and step["stderr"]:
            lines.append(f"  - 错误：{step['stderr'][:500]}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local Liepin reply intelligence steps.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--read-liepin", action="store_true", help="Read visible Liepin IM conversations through local Chrome CDP.")
    parser.add_argument("--collect-talk-samples", action="store_true", help="Collect visible Liepin IM preview talk samples without opening conversations.")
    parser.add_argument("--chrome-port", type=int, default=9223)
    parser.add_argument("--build-app", action="store_true", help="Rebuild and install the local desktop workstation app.")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    py = sys.executable
    steps: list[dict] = []

    if args.read_liepin:
        steps.append(
            run_step(
                "读取猎聘 IM 可见回复",
                [
                    py,
                    str(SCRIPTS_DIR / "liepin_im_reply_ingest.py"),
                    "--db",
                    str(db_path),
                    "--output-dir",
                    str(output_dir),
                    "--port",
                    str(args.chrome_port),
                    "--mode",
                    "ingest",
                    "--confirm",
                    "INGEST",
                ],
            )
        )

    if args.collect_talk_samples:
        steps.append(
            run_step(
                "采集猎聘历史话术样本",
                [
                    py,
                    str(SCRIPTS_DIR / "collect_liepin_talk_samples.py"),
                    "--db",
                    str(db_path),
                    "--output-dir",
                    str(output_dir),
                    "--port",
                    str(args.chrome_port),
                    "--save-db",
                ],
            )
        )

    steps.append(
        run_step(
            "确认项目字段底座",
            [
                py,
                str(SCRIPTS_DIR / "ensure_project_confirmation_schema.py"),
                "--db",
                str(db_path),
                "--output-dir",
                str(output_dir),
            ],
        )
    )
    steps.append(
        run_step(
            "确认话术质量字段底座",
            [
                py,
                str(SCRIPTS_DIR / "ensure_talk_quality_schema.py"),
                "--db",
                str(db_path),
                "--output-dir",
                str(output_dir),
            ],
        )
    )
    steps.append(
        run_step(
            "增强回复项目判断与话术",
            [
                py,
                str(SCRIPTS_DIR / "enhance_followup_intelligence.py"),
                "--db",
                str(db_path),
                "--output-dir",
                str(output_dir),
            ],
        )
    )
    steps.append(
        run_step(
            "构建历史话术算法",
            [
                py,
                str(SCRIPTS_DIR / "build_talk_algorithm.py"),
                "--db",
                str(db_path),
                "--output-dir",
                str(output_dir),
            ],
        )
    )
    steps.append(
        run_step(
            "写入回复学习记录",
            [
                py,
                str(SCRIPTS_DIR / "record_reply_learning_notes.py"),
                "--db",
                str(db_path),
                "--output-dir",
                str(output_dir),
            ],
        )
    )
    steps.append(
        run_step(
            "生成跟进话术草稿",
            [
                py,
                str(SCRIPTS_DIR / "generate_talk_drafts.py"),
                "--db",
                str(db_path),
                "--output-dir",
                str(output_dir),
            ],
        )
    )
    steps.append(
        run_step(
            "生成话术质量报告",
            [
                py,
                str(SCRIPTS_DIR / "generate_talk_quality_report.py"),
                "--db",
                str(db_path),
                "--output-dir",
                str(output_dir),
            ],
        )
    )
    steps.append(
        run_step(
            "生成回复智能驾驶舱",
            [
                py,
                str(SCRIPTS_DIR / "generate_reply_dashboard.py"),
                "--db",
                str(db_path),
                "--output-dir",
                str(output_dir),
            ],
        )
    )

    if args.build_app:
        steps.append(run_step("编译并安装桌面工作台", ["bash", str(PNX_BUILD_SCRIPT)], cwd=PNX_BUILD_SCRIPT.parents[1]))

    report = write_run_report(steps, output_dir)
    ok = all(step["ok"] for step in steps)
    print(
        json.dumps(
            {
                "ok": ok,
                "db": str(db_path),
                "steps": [summarize_step(step) for step in steps],
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
