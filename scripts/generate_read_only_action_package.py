#!/usr/bin/env python3
"""Build a read-only action package from the latest Liepin summary outputs."""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = BASE_DIR / "outputs"
DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_PRIVATE_VAULT = DEFAULT_OUTPUT_DIR / "_read_only_private_vault"

THIN_JOBS = [
    "分析设备专家",
    "量测设备专家",
    "IT运维管理专家",
    "CVD工艺专家",
    "YE工程师",
]

TARGET_COMPANY_FALLBACK = ["中芯国际", "华虹", "晶合集成", "粤芯", "台积电南京", "长鑫/长存"]


@dataclass
class QueueItem:
    priority: str
    score: int
    candidate: str
    project: str
    action: str
    source: str
    backlink: str


def latest_file(output_dir: Path, pattern: str) -> Path | None:
    files = sorted(output_dir.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)
    return files[0] if files else None


def stamped_file(output_dir: Path, prefix: str, stamp: str | None) -> Path | None:
    if not stamp:
        return None
    path = output_dir / f"{prefix}_{stamp}.md"
    return path if path.exists() else None


def load_text(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def parse_waitlist(path: Path) -> list[QueueItem]:
    rows: list[QueueItem] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| P"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 7:
            continue
        rows.append(
            QueueItem(
                priority=cells[0],
                score=int(float(cells[1])),
                candidate=cells[2],
                project=cells[3],
                action=cells[4],
                source=cells[5],
                backlink=cells[6],
            )
        )
    return rows


def project_focus(project: str) -> str:
    text = project.lower()
    if "pqe" in text or "质量" in project:
        return "确认 12 寸 Fab / PQE / 客诉闭环 / 制程质量锚点是否成立"
    if "fpga" in text:
        return "确认 FPGA 方向、主芯片/平台、带队或量产经历是否匹配"
    if "硬件" in project:
        return "确认模拟/数字硬件栈、团队规模、产品线是否对口"
    if "机械" in project:
        return "确认设备机械/运动机构/精密设计经历是否连续"
    if "电源" in project or "acdc" in text:
        return "确认电源拓扑、服务器/工业电源场景、管理跨度是否满足"
    if "device" in text:
        return "确认 Device/器件工艺边界、制程节点和平台归属"
    if "运动台" in project:
        return "确认运动台产品/平台、应用场景和跨团队交付经验"
    if "cvd" in text:
        return "确认 CVD 工艺深度、量产节点和设备平台经验"
    return "确认当前公司、岗位锚点、半导体相关度和推进边界"


def concrete_next_step(item: QueueItem) -> str:
    focus = project_focus(item.project)
    if "发起沟通" in item.action:
        return f"只做简历复核和话术预判：{focus}；通过后再保留为待发送，不外发。"
    if "可转微信" in item.action:
        return "先补 3 个口径：求职意愿、地点、薪资；确认后再进入可执行，不做真实转发。"
    if "补公司/岗位关键信息" in item.action:
        return f"只补 1 个项目锚点：{focus}；补完回到推荐前校验，不扩成整段追问。"
    if "补一轮硬性门槛确认" in item.action:
        return f"先核硬门槛：{focus}；结论只记为通过/不通过/待补，不推进外发。"
    if "可继续沟通" in item.action:
        return f"先做门槛复核：{focus}；如果仍成立，只保留继续沟通草稿。"
    return f"先做本地复核：{focus}；确认前不发送、不写回。"


def classify_groups(items: list[QueueItem]) -> dict[str, list[QueueItem]]:
    groups = {"今日先看": [], "可批量处理": [], "暂缓沉淀": []}
    for idx, item in enumerate(items):
        if idx < 10:
            groups["今日先看"].append(item)
        elif item.score >= 78:
            groups["可批量处理"].append(item)
        else:
            groups["暂缓沉淀"].append(item)
    return groups


def parse_json_list(text: str | None) -> list[str]:
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def thin_job_expansion(job: str) -> tuple[list[str], list[str], list[str], list[str]]:
    if job == "分析设备专家":
        return (
            ["TEM", "FIB", "SIMS", "failure analysis", "lab tool maintenance"],
            ["中科飞测", "KLA", "Applied Materials", "ASML/HMI"],
            ["纯生产设备不含分析机台", "非 12 寸 Fab 仅泛化实验室", "纯售后但无驻厂深经验"],
            ["当前公司", "分析机台类型", "驻厂/量产场景", "维修保养深度", "团队管理跨度"],
        )
    if job == "量测设备专家":
        return (
            ["Metrology", "量检测", "overlay", "CD-SEM", "defect inspection"],
            ["中科飞测", "KLA", "Hitachi High-Tech", "Onto Innovation"],
            ["纯工艺不含量测设备", "只做研发测试无量产现场", "无跨部门推动经验"],
            ["当前公司", "量测机台类型", "工艺/材料背景", "带队经历", "量产闭环案例"],
        )
    if job == "IT运维管理专家":
        return (
            ["ITIL", "SRE", "高可用集群", "半导体制造 IT 运维", "数据库/网络/安全"],
            ["中芯国际", "华虹", "长鑫", "长存", "士兰微", "晶合集成"],
            ["纯开发无运维治理", "只做单一桌面支持", "无大型制造业/半导体场景"],
            ["当前公司", "运维域", "ITIL/SRE 实践", "集群规模", "管理团队人数"],
        )
    if job == "CVD工艺专家":
        return (
            ["CVD", "化学气相沉积", "薄膜沉积", "PECVD", "LPCVD"],
            ["北方华创", "拓荆科技", "中微公司", "中芯国际", "华虹", "长鑫/长存"],
            ["年限不足 10 年", "缺 CVD 量产平台", "纯器件端无设备/工艺协同"],
            ["当前公司", "CVD 平台", "量产节点", "团队管理", "工艺问题闭环案例"],
        )
    return (
        ["Yield Enhancement", "YE", "良率提升", "SPC", "失效分析"],
        ["中芯国际", "华虹", "长鑫", "长存", "粤芯", "晶合集成"],
        ["纯质量体系无良率实战", "无 12 寸 Fab 场景", "只做单点异常无系统改善"],
        ["当前公司", "良率提升模块", "工艺/质量交叉背景", "带队经历", "闭环项目案例"],
    )


def load_thin_job_profiles(db_path: Path) -> list[dict[str, object]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows: list[dict[str, object]] = []
        for job in THIN_JOBS:
            row = conn.execute(
                """
                SELECT position, hard_requirements_json, ability_keywords_json,
                       target_companies_json, exclusion_tags_json, search_keywords_json,
                       soft_preferences_json, jd_analysis_summary
                FROM position_profiles
                WHERE client = '鹏新旭' AND position = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (job,),
            ).fetchone()
            if row is None:
                rows.append({"position": job})
                continue
            extra_keywords, inferred_companies, inferred_exclusions, expected_fields = thin_job_expansion(job)
            target_companies = parse_json_list(row["target_companies_json"]) or TARGET_COMPANY_FALLBACK
            exclusions = parse_json_list(row["exclusion_tags_json"]) + inferred_exclusions
            keywords = parse_json_list(row["search_keywords_json"]) + parse_json_list(row["ability_keywords_json"]) + extra_keywords
            rows.append(
                {
                    "position": job,
                    "summary": row["jd_analysis_summary"] or "",
                    "hard_requirements": parse_json_list(row["hard_requirements_json"]),
                    "keywords": list(dict.fromkeys([item for item in keywords if item]))[:10],
                    "target_companies": list(dict.fromkeys(target_companies + inferred_companies))[:12],
                    "exclusions": list(dict.fromkeys([item for item in exclusions if item]))[:8],
                    "expected_fields": expected_fields,
                }
            )
        return rows
    finally:
        conn.close()


def source_freshness(output_dir: Path) -> list[dict[str, str]]:
    targets = [
        ("今日优先处理人选", latest_file(output_dir, "猎聘今日优先处理人选_*.md")),
        ("岗位推进入口", latest_file(output_dir, "猎聘岗位推进入口_*.md")),
        ("客户推荐汇总", latest_file(output_dir, "猎聘客户推荐汇总_*.md")),
        ("推荐前校验", latest_file(output_dir, "猎聘推荐前校验_*.md")),
    ]
    payload: list[dict[str, str]] = []
    for label, path in targets:
        payload.append(
            {
                "label": label,
                "path": str(path) if path else "未生成",
                "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds") if path else "未生成",
            }
        )
    return payload


def write_outputs(
    output_dir: Path,
    private_vault: Path,
    stamp: str,
    body: str,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = output_dir / f"猎聘今日行动清单_{stamp}.md"
    report.write_text(body, encoding="utf-8")
    private_dir = private_vault / "60_Reviews"
    private_dir.mkdir(parents=True, exist_ok=True)
    private_path = private_dir / "今日行动清单.md"
    private_path.write_text(body, encoding="utf-8")
    return report, private_path


def build_report(
    waitlist_path: Path,
    summary_path: Path,
    manual_path: Path,
    ops_path: Path,
    thin_jobs: list[dict[str, object]],
    freshness: list[dict[str, str]],
) -> tuple[str, dict[str, int]]:
    items = parse_waitlist(waitlist_path)
    groups = classify_groups(items)
    summary_text = load_text(summary_path)
    manual_text = load_text(manual_path)
    counts = {name: len(rows) for name, rows in groups.items()}

    lines = [
        "# 猎聘今日行动清单",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "这是基于 2026-07-02 只读刷新结果整理出的本地行动包。",
        "边界：只做本地复核、search-only 或 dry-run；不打开浏览器，不触达候选人，不外发，不写库。",
        "",
        "## 清单总览",
        "",
        f"- 待确认总数：{len(items)}",
        f"- 今日先看：{counts['今日先看']}",
        f"- 可批量处理：{counts['可批量处理']}",
        f"- 暂缓沉淀：{counts['暂缓沉淀']}",
        "",
        "## 分组规则",
        "",
        "- 今日先看：直接取优先级最高且最接近真实动作的前 10 条，优先做简历复核、项目锚点确认和微信边界确认。",
        "- 可批量处理：其余分数 >= 78 的条目，适合按同类动作合并处理。",
        "- 暂缓沉淀：分数 <= 77 的 P1 尾部条目，先保留为低优先级复核池。",
        "",
        "## 今日先看（前 10 条）",
        "",
        "| 顺位 | 分数 | 候选人 | 项目 | 当前动作 | 具体下一步 |",
        "|---:|---:|---|---|---|---|",
    ]
    for idx, item in enumerate(groups["今日先看"], start=1):
        lines.append(
            f"| {idx} | {item.score} | {item.candidate} | {item.project} | {item.action} | {concrete_next_step(item)} |"
        )

    lines.extend(["", "## 可批量处理", ""])
    for item in groups["可批量处理"]:
        lines.append(f"- {item.priority} {item.score}｜{item.candidate}｜{item.project}｜{item.action}")

    lines.extend(["", "## 暂缓沉淀", ""])
    for item in groups["暂缓沉淀"]:
        lines.append(f"- {item.priority} {item.score}｜{item.candidate}｜{item.project}｜{item.action}")

    lines.extend(["", "## 薄岗补搜计划（search-only / dry-run）", ""])
    for plan in thin_jobs:
        lines.extend(
            [
                f"### 鹏新旭 / {plan['position']}",
                "",
                f"- 岗位摘要：{plan.get('summary') or '本地画像未补充摘要'}",
                f"- 关键词：{'、'.join(plan.get('keywords') or []) or '待补'}",
                f"- 目标公司池：{'、'.join(plan.get('target_companies') or []) or '待补'}",
                f"- 排除项：{'、'.join(plan.get('exclusions') or []) or '待补'}",
                f"- 预期输出字段：{'、'.join(plan.get('expected_fields') or []) or '待补'}",
                "",
            ]
        )

    lines.extend(
        [
            "## 底层来源新鲜度",
            "",
            "| 来源 | 最近文件 | 修改时间 |",
            "|---|---|---|",
        ]
    )
    for item in freshness:
        lines.append(f"| {item['label']} | {item['path']} | {item['mtime']} |")

    lines.extend(
        [
            "",
            "## 为什么上一轮会吃到 2026-06-26 历史产物",
            "",
            "- `refresh_liepin_intelligence.py --read-only` 只重做摘要层文件，不重算 `今日优先处理人选`、`推荐前校验`、`客户推荐汇总`、`岗位推进入口`。",
            "- `generate_confirmation_queue.py` 和 `generate_daily_ops_console.py` 都是通过 `latest_file(...)` 直接读取现有 `outputs`，所以会拿到工作区里最后一次生成的旧文件。",
            "- 当前本地 DB 还在更新，但摘要层如果不先重算源文件，就会继续引用旧批次产物。",
            "",
            "## 本轮使用入口",
            "",
            f"- 待确认清单：[{waitlist_path.name}]({waitlist_path})",
            f"- 每日拍板摘要：[{summary_path.name}]({summary_path})",
            f"- 确认后执行手册：[{manual_path.name}]({manual_path})",
            f"- 日常操作台：[猎聘日常操作台.md]({ops_path})",
            "",
            "## 只读边界提醒",
            "",
            "- 本文件只把动作拆成可执行检查项，不触发真实发送。",
            "- 薄岗补搜计划只用于下一轮 search-only / dry-run，不直接开搜或入库。",
            "- 任何归属修正、触达和反馈写入，都必须另行显式确认。",
            "",
            "## 摘要来源片段",
            "",
            summary_text.strip() or "未读取到摘要文件。",
            "",
            manual_text.strip() or "未读取到执行手册文件。",
        ]
    )
    return "\n".join(lines).rstrip() + "\n", counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a read-only action package from latest outputs.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--private-vault", default=str(DEFAULT_PRIVATE_VAULT))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--stamp")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser()
    stamp = args.stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    waitlist_path = stamped_file(output_dir, "猎聘待确认清单", args.stamp) or latest_file(output_dir, "猎聘待确认清单_*.md")
    summary_path = stamped_file(output_dir, "猎聘每日拍板摘要", args.stamp) or latest_file(output_dir, "猎聘每日拍板摘要_*.md")
    manual_path = stamped_file(output_dir, "猎聘确认后执行手册", args.stamp) or latest_file(output_dir, "猎聘确认后执行手册_*.md")
    ops_path = BASE_DIR / "猎聘日常操作台.md"
    if not waitlist_path or not summary_path or not manual_path:
        raise SystemExit("缺少待确认清单 / 每日拍板摘要 / 确认后执行手册，无法生成行动包。")

    thin_jobs = load_thin_job_profiles(Path(args.db).expanduser())
    freshness = source_freshness(output_dir)
    body, counts = build_report(waitlist_path, summary_path, manual_path, ops_path, thin_jobs, freshness)
    report, private_path = write_outputs(output_dir, Path(args.private_vault).expanduser(), stamp, body)
    print(
        json.dumps(
            {
                "ok": True,
                "report": str(report),
                "private": str(private_path),
                "counts": counts,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
