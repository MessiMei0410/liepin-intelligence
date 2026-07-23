#!/usr/bin/env python3
"""Aggregate read-only OpenCLI shadow A/B rounds into a weekly trend report.

Inputs are the persisted shadow artifacts written by the channel adapters
(work/*-opencli-ab-*.json, one file per round). The report is Markdown and
never persists candidate PII: the red-line scanner reports offending field
paths only, never their values.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ASA_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ASA_ROOT / "work"
DEFAULT_REPORT_DIR = ASA_ROOT / "experiments" / "reports"
ROUND_GLOB = "*-opencli-ab-*.json"

CHANNEL_LABELS = {"liepin": "猎聘", "xsaas": "X-SaaS"}
CHANNEL_ORDER = ("liepin", "xsaas")

# Red-line rules: shadow artifacts must never persist candidate names, external
# IDs, URLs, resume body text, or CDP session values. Keys are matched
# case-insensitively; values are never copied into findings.
REDLINE_KEY_RULES = {
    "姓名": {"name", "candidate_name", "real_name", "first_name", "last_name"},
    "外部ID": {
        "candidate_id", "resumeid", "resume_id", "res_id_encode",
        "xsaas_id", "external_id", "rid",
    },
    "URL": {"url", "resume_url", "source_url", "profile_url", "link", "homepage"},
    "简历正文": {
        "profile_text", "full_text", "work_text", "project_text",
        "education_text", "resume_text", "resume_content", "cv_text",
    },
    "CDP会话值": {
        "websocketdebuggerurl", "target_id", "sessionid", "session_id",
        "cdp_session", "cookie", "cookies",
    },
}
REDLINE_ALLOWED_KEYS = {"target_id_hash"}  # sha256-truncated, not a session value
URL_VALUE_RE = re.compile(r"^\s*(https?|wss?)://", re.IGNORECASE)

EXPECTED_MODE = "read_only_no_intake_no_outreach"

GATE_ITEM_LABELS = [
    ("stability_strictly_better", "稳定性严格更高"),
    ("relative_recall_strictly_better", "相对召回严格更高"),
    ("field_completeness_not_worse", "字段完整率不差"),
    ("reuse_asa_intake_audit", "复用 ASA 审批排重 / intake 归因审计"),
    ("independent_pilot_first", "先独立试点"),
]


def clean_number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def detect_channel(payload: dict[str, Any], path: Path) -> str:
    channel = str(payload.get("channel") or "").strip().lower()
    if channel in CHANNEL_LABELS:
        return channel
    name = path.name.lower()
    for candidate in CHANNEL_ORDER:
        if name.startswith(f"{candidate}-") or f"-{candidate}-" in name:
            return candidate
    return "unknown"


def scan_redline(payload: Any, source: str) -> list[dict[str, str]]:
    """Walk a shadow artifact and flag forbidden field names / URL values.

    Findings carry the JSON path and category only — values are deliberately
    never copied, so the report itself stays free of the flagged data.
    """
    findings: list[dict[str, str]] = []

    def visit(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                lowered = str(key).strip().lower()
                child_path = f"{path}.{key}" if path else str(key)
                if lowered not in REDLINE_ALLOWED_KEYS:
                    for category, keys in REDLINE_KEY_RULES.items():
                        if lowered in keys:
                            findings.append({
                                "source": source,
                                "json_path": child_path,
                                "category": category,
                                "detail": f"疑似{category}字段 '{key}'",
                            })
                            break
                if isinstance(value, str) and URL_VALUE_RE.search(value):
                    findings.append({
                        "source": source,
                        "json_path": child_path,
                        "category": "URL",
                        "detail": f"字段 '{key}' 的值疑似 URL",
                    })
                visit(value, child_path)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                visit(item, f"{path}[{index}]")

    visit(payload, "")
    return findings


def judge(opencli_value: float | None, baseline_value: float | None) -> str:
    """Strict gate semantics: strictly higher is 更优, lower is 更劣, else 持平."""
    if opencli_value is None or baseline_value is None:
        return "数据不足"
    if opencli_value > baseline_value:
        return "更优"
    if opencli_value < baseline_value:
        return "更劣"
    return "持平"


def load_round(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} is not a JSON object")
    baseline = payload.get("baseline") if isinstance(payload.get("baseline"), dict) else {}
    opencli = payload.get("opencli") if isinstance(payload.get("opencli"), dict) else {}
    comparison = payload.get("comparison") if isinstance(payload.get("comparison"), dict) else {}
    baseline_ms = clean_number(baseline.get("mean_duration_ms"))
    opencli_ms = clean_number(opencli.get("mean_duration_ms"))
    speedup = None
    if baseline_ms and opencli_ms is not None:
        speedup = round((baseline_ms - opencli_ms) / baseline_ms * 100, 1)
    return {
        "source": path.name,
        "channel": detect_channel(payload, path),
        "generated_at": str(payload.get("generated_at") or ""),
        "mode": str(payload.get("mode") or ""),
        "repeats": payload.get("repeats"),
        "baseline_success_rate": clean_number(baseline.get("success_rate")),
        "opencli_success_rate": clean_number(opencli.get("success_rate")),
        "baseline_stability": clean_number(comparison.get("baseline_stability_score")),
        "opencli_stability": clean_number(comparison.get("opencli_stability_score")),
        "baseline_recall": clean_number(comparison.get("baseline_relative_recall")),
        "opencli_recall": clean_number(comparison.get("opencli_relative_recall")),
        "baseline_field_completeness": clean_number(baseline.get("field_completeness")),
        "opencli_field_completeness": clean_number(opencli.get("field_completeness")),
        "baseline_mean_duration_ms": baseline_ms,
        "opencli_mean_duration_ms": opencli_ms,
        "opencli_speedup_percent": speedup,
        "stability_judgment": judge(
            clean_number(comparison.get("opencli_stability_score")),
            clean_number(comparison.get("baseline_stability_score")),
        ),
        "recall_judgment": judge(
            clean_number(comparison.get("opencli_relative_recall")),
            clean_number(comparison.get("baseline_relative_recall")),
        ),
        "field_completeness_judgment": judge(
            clean_number(opencli.get("field_completeness")),
            clean_number(baseline.get("field_completeness")),
        ),
        "redline_findings": scan_redline(payload, path.name),
    }


def gate_status(values: list[bool | None]) -> str:
    """Aggregate one gate item across rounds: 已满足 / 未满足 / 数据不足."""
    present = [value for value in values if value is not None]
    if not present:
        return "数据不足"
    return "已满足" if all(present) else "未满足"


def channel_gate(channel_rounds: list[dict[str, Any]]) -> dict[str, str]:
    if not channel_rounds:
        return {
            "stability_strictly_better": "数据不足",
            "relative_recall_strictly_better": "数据不足",
            "field_completeness_not_worse": "数据不足",
        }
    return {
        "stability_strictly_better": gate_status([
            None if r["stability_judgment"] == "数据不足" else r["stability_judgment"] == "更优"
            for r in channel_rounds
        ]),
        "relative_recall_strictly_better": gate_status([
            None if r["recall_judgment"] == "数据不足" else r["recall_judgment"] == "更优"
            for r in channel_rounds
        ]),
        "field_completeness_not_worse": gate_status([
            None if r["field_completeness_judgment"] == "数据不足" else r["field_completeness_judgment"] != "更劣"
            for r in channel_rounds
        ]),
    }


def aggregate(data_dir: Path) -> dict[str, Any]:
    files = sorted(data_dir.glob(ROUND_GLOB))
    rounds: list[dict[str, Any]] = []
    for path in files:
        try:
            rounds.append(load_round(path))
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"warning: skipped unreadable round file {path}: {exc}", file=sys.stderr)
    rounds.sort(key=lambda item: (item["generated_at"], item["source"]))

    channels: dict[str, Any] = {}
    for channel in CHANNEL_ORDER:
        channel_rounds = [r for r in rounds if r["channel"] == channel]
        judgments = [r["stability_judgment"] for r in channel_rounds] + [
            r["recall_judgment"] for r in channel_rounds
        ]
        if not channel_rounds:
            trend = "无数据"
        elif len(channel_rounds) == 1:
            trend = "仅一轮，不足以判断趋势"
        elif any(value == "更劣" for value in judgments):
            trend = "出现更劣轮次"
        elif all(value == "更优" for value in judgments):
            trend = "持续严格更优"
        else:
            trend = "持平为主，未持续严格更优"
        channels[channel] = {
            "label": CHANNEL_LABELS[channel],
            "rounds": channel_rounds,
            "trend": trend,
            "gate": channel_gate(channel_rounds),
        }

    all_findings = [finding for r in rounds for finding in r["redline_findings"]]
    mode_values = {r["mode"] for r in rounds}
    intake_audit_status = "数据不足"
    intake_audit_note = (
        "影子执行为只读（mode=read_only_no_intake_no_outreach），不经过 ASA "
        "审批排重与 intake 归因审计链路；A/B 数据无法证明复用能力，需独立试点接入验证。"
    )
    if rounds and mode_values != {EXPECTED_MODE}:
        intake_audit_status = "未满足"
        intake_audit_note = "存在 mode 非只读约定的轮次，须先排查该轮影子产物。"
    elif not rounds:
        intake_audit_note = "无任何轮次数据。"

    gate_items: dict[str, dict[str, str]] = {}
    for key, label in GATE_ITEM_LABELS[:3]:
        statuses = [channels[channel]["gate"][key] for channel in CHANNEL_ORDER]
        known = [status for status in statuses if status != "数据不足"]
        if not known:
            status = "数据不足"
        elif all(status == "已满足" for status in statuses):
            status = "已满足"
        else:
            status = "未满足"
        gate_items[key] = {
            "label": label,
            "status": status,
            "note": "；".join(
                f"{channels[channel]['label']}:{channels[channel]['gate'][key]}"
                for channel in CHANNEL_ORDER
            ),
        }
    gate_items["reuse_asa_intake_audit"] = {
        "label": GATE_ITEM_LABELS[3][1], "status": intake_audit_status, "note": intake_audit_note,
    }
    gate_items["independent_pilot_first"] = {
        "label": GATE_ITEM_LABELS[4][1], "status": "需书面评估",
        "note": "流程项，不随数据自动判定；迁移前需书面评估与审批记录。",
    }

    data_satisfied = all(
        gate_items[key]["status"] == "已满足" for key, _ in GATE_ITEM_LABELS[:3]
    ) and gate_items["reuse_asa_intake_audit"]["status"] == "已满足"
    decision = (
        "数据项已全部满足，仍须完成流程项书面评估后方可独立试点"
        if data_satisfied
        else "门槛仍关闭，保留现有执行器（keep_existing_executor）"
    )
    timestamps = [r["generated_at"] for r in rounds if r["generated_at"]]
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_dir": str(data_dir),
        "round_count": len(rounds),
        "data_window": [min(timestamps), max(timestamps)] if timestamps else [],
        "channels": channels,
        "gate_items": gate_items,
        "decision": decision,
        "redline_findings": all_findings,
        "redline_ok": not all_findings,
    }


def fmt(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def fmt_pair(opencli_value: float | None, baseline_value: float | None) -> str:
    return f"{fmt(baseline_value)}/{fmt(opencli_value)}"


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# OpenCLI 影子对照周度趋势报告")
    lines.append("")
    lines.append(f"- 生成时间：{report['generated_at']}")
    lines.append(f"- 数据目录：`{report['data_dir']}`")
    if report["data_window"]:
        lines.append(f"- 数据窗口：{report['data_window'][0]} ~ {report['data_window'][1]}")
    lines.append(f"- 轮次总数：{report['round_count']}")
    lines.append("- 影子契约：只读影子寻访执行器，不影响 intake / outreach，样本策略 first_query_per_channel")
    lines.append("")

    lines.append("## 红线扫描")
    lines.append("")
    if report["redline_ok"]:
        lines.append("未发现疑似违规字段（姓名 / 外部ID / URL / 简历正文 / CDP 会话值）。")
    else:
        lines.append(f"> **⚠ 告警：发现 {len(report['redline_findings'])} 处疑似违规字段，影子产物违反红线约定，须立即核查并清除！**")
        lines.append("")
        lines.append("| 文件 | JSON 路径 | 类别 | 说明 |")
        lines.append("| --- | --- | --- | --- |")
        for finding in report["redline_findings"]:
            lines.append(
                f"| {finding['source']} | `{finding['json_path']}` "
                f"| {finding['category']} | {finding['detail']} |"
            )
    lines.append("")

    for channel in CHANNEL_ORDER:
        section = report["channels"][channel]
        rounds = section["rounds"]
        lines.append(f"## 渠道：{section['label']}（{len(rounds)} 轮）")
        lines.append("")
        if not rounds:
            lines.append("无该渠道的影子对照数据。")
            lines.append("")
            continue
        lines.append("| 轮次 | 时间 | repeats | 成功率 基线/OpenCLI | 稳定分 基线/OpenCLI | 稳定判断 | 相对召回 基线/OpenCLI | 召回判断 | 字段完整率 基线/OpenCLI | 完整率判断 | 耗时ms 基线/OpenCLI | OpenCLI 提速 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for index, r in enumerate(rounds, 1):
            speedup = (
                f"{r['opencli_speedup_percent']:+.1f}%"
                if r["opencli_speedup_percent"] is not None else "—"
            )
            base_ms = r["baseline_mean_duration_ms"]
            open_ms = r["opencli_mean_duration_ms"]
            duration = (
                f"{int(base_ms)}/{int(open_ms)}"
                if base_ms is not None and open_ms is not None else "—"
            )
            lines.append(
                f"| {index} | {r['generated_at'] or '—'} | {r['repeats'] if r['repeats'] is not None else '—'} "
                f"| {fmt_pair(r['opencli_success_rate'], r['baseline_success_rate'])} "
                f"| {fmt_pair(r['opencli_stability'], r['baseline_stability'])} | {r['stability_judgment']} "
                f"| {fmt_pair(r['opencli_recall'], r['baseline_recall'])} | {r['recall_judgment']} "
                f"| {fmt_pair(r['opencli_field_completeness'], r['baseline_field_completeness'])} | {r['field_completeness_judgment']} "
                f"| {duration} | {speedup} |"
            )
        lines.append("")
        lines.append(f"**趋势判断：{section['trend']}**")
        if len(rounds) == 1:
            lines.append("")
            lines.append("（仅一轮数据，所有趋势结论按'仅一轮'标注，不作为迁移依据。）")
        lines.append("")

    lines.append("## 迁移门槛五项结论")
    lines.append("")
    lines.append("| 门槛项 | 结论 | 说明 |")
    lines.append("| --- | --- | --- |")
    for key, _label in GATE_ITEM_LABELS:
        item = report["gate_items"][key]
        lines.append(f"| {item['label']} | {item['status']} | {item['note']} |")
    lines.append("")
    lines.append(f"**总体结论：{report['decision']}**")
    lines.append("")
    lines.append("## 口径说明")
    lines.append("")
    lines.append("- 趋势判断严格按迁移门槛口径：稳定性与相对召回须**严格更高**才算'更优'，持平不算过；字段完整率'不差'（≥）即该项不扣分。")
    lines.append("- 相对召回以双引擎去重并集为对照集（overlap/union），非标注真值召回。")
    lines.append("- 耗时与提速仅为参考项，不构成门槛项。")
    lines.append("- 红线扫描只记录疑似字段路径与类别，不复制字段值。")
    lines.append("")
    return "\n".join(lines)


def default_output_path() -> Path:
    week = datetime.now().strftime("%G-W%V")
    return DEFAULT_REPORT_DIR / f"opencli_shadow_trend_{week}.md"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate OpenCLI shadow A/B rounds into a weekly trend report"
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    data_dir = args.data_dir.expanduser().resolve()
    report = aggregate(data_dir)
    markdown = render_markdown(report)
    output = (args.out or default_output_path()).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown, encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "output": str(output),
        "round_count": report["round_count"],
        "data_window": report["data_window"],
        "redline_ok": report["redline_ok"],
        "redline_findings": len(report["redline_findings"]),
        "decision": report["decision"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
