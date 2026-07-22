#!/usr/bin/env python3
"""Build a reusable talk algorithm from Liepin talk samples."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


STYLE_RULES = [
    "语气短、自然、像真人猎头，不写长段营销文。",
    "开头承接候选人原话，不急着卖岗位。",
    "一条消息尽量只问一个主问题，避免一次性问薪资、地点、动机、时间。",
    "首轮要保留客户/岗位/方向中的一个锚点，让候选人知道不是群发。",
    "能加微信时顺势转私域，但不强迫、不替用户发送。",
    "候选人问公司或年限时，先补关键信息，只追一个年限或电话问题。",
    "候选人正向匹配时，优先约 10 分钟沟通，不把动机、薪资、地点一次性塞进去。",
    "拒绝或不对口时，不强推，记录原因并换方向。",
]


STRATEGY_RULES = {
    "broad_semiconductor_wechat": {
        "stage": "cold_open",
        "goal": "低成本建立联系",
        "pattern": "半导体方向 + 加微信 + 看机会随时沟通",
        "risk": "过于宽泛，容易让候选人反问公司/岗位或被判断为群发。",
        "upgrade": "加入岗位名、客户类型、候选人头衔里的一个关键词。",
    },
    "asks_company": {
        "stage": "info_request",
        "goal": "补足透明度，减少不确定感",
        "pattern": "先回答公司/岗位/年限范围，再只问一个年限或电话问题。",
        "risk": "如果仍含糊，候选人会流失。",
        "upgrade": "明确客户可透露程度；不可透露时说明原因，并给岗位方向和核心要求。",
    },
    "positive_fit": {
        "stage": "qualified_interest",
        "goal": "快速转入电话或微信深聊",
        "pattern": "认可匹配点 + 约 10 分钟电话，把岗位重点和经历快速对齐。",
        "risk": "只回复“可以聊”会浪费一次高意向窗口。",
        "upgrade": "电话或微信后再分步补薪资、地点、动机。",
    },
    "contact_exchange": {
        "stage": "channel_shift",
        "goal": "承接联系方式/简历，转入稳定沟通",
        "pattern": "我加您 + 一句机会锚点 + 微信里发岗位要点。",
        "risk": "只收联系方式不问关键信息，后续需要二次追问。",
        "upgrade": "先建立沟通，再分步确认地点、薪资和意愿。",
    },
    "salary": {
        "stage": "salary_probe",
        "goal": "确认薪资结构与可谈空间",
        "pattern": "先确认当前总包、固定/奖金结构、期望区间，再判断客户预算。",
        "risk": "过早承诺薪资会被动。",
        "upgrade": "只做区间判断，不替客户承诺。",
    },
    "mismatch_or_reject": {
        "stage": "graceful_close",
        "goal": "保留关系并沉淀排除原因",
        "pattern": "感谢反馈 + 记录方向不匹配 + 后续有更贴合机会再沟通。",
        "risk": "继续追问会损伤关系。",
        "upgrade": "问一句未来可关注方向，若对方愿意再记录。",
    },
}


def connect_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def load_samples(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT candidate_name, candidate_title, direction_guess, strategy_guess, message
        FROM talk_samples
        ORDER BY id
        """
    ).fetchall()


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS talk_algorithm_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_key TEXT NOT NULL UNIQUE,
            stage TEXT,
            goal TEXT,
            pattern TEXT,
            risk TEXT,
            upgrade TEXT,
            sample_count INTEGER DEFAULT 0,
            positive_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
        """
    )
    conn.commit()


def save_rules(conn: sqlite3.Connection, samples: list[sqlite3.Row]) -> list[dict]:
    counts = Counter(row["strategy_guess"] for row in samples)
    positive_counts = Counter(
        row["strategy_guess"]
        for row in samples
        if row["strategy_guess"] in {"positive_fit", "contact_exchange", "asks_company", "salary"}
    )
    rows: list[dict] = []
    for key, rule in STRATEGY_RULES.items():
        sample_count = counts.get(key, 0)
        positive_count = positive_counts.get(key, 0)
        conn.execute(
            """
            INSERT INTO talk_algorithm_rules
                (strategy_key, stage, goal, pattern, risk, upgrade, sample_count, positive_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
            ON CONFLICT(strategy_key) DO UPDATE SET
                stage = excluded.stage,
                goal = excluded.goal,
                pattern = excluded.pattern,
                risk = excluded.risk,
                upgrade = excluded.upgrade,
                sample_count = excluded.sample_count,
                positive_count = excluded.positive_count,
                updated_at = datetime('now','localtime')
            """,
            (
                key,
                rule["stage"],
                rule["goal"],
                rule["pattern"],
                rule["risk"],
                rule["upgrade"],
                sample_count,
                positive_count,
            ),
        )
        rows.append({"key": key, "sample_count": sample_count, "positive_count": positive_count, **rule})
    conn.commit()
    return rows


def write_report(samples: list[sqlite3.Row], rules: list[dict], output_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘话术算法说明_{stamp}.md"
    direction_counts = Counter(row["direction_guess"] for row in samples)
    strategy_counts = Counter(row["strategy_guess"] for row in samples)
    self_messages = Counter(row["message"] for row in samples if row["direction_guess"] == "self")
    lines = [
        "# 猎聘话术算法说明",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 样本结论",
        "",
        f"- 样本总数：{len(samples)}",
        f"- 方向分布：{'、'.join(f'{k} {v}' for k, v in sorted(direction_counts.items()))}",
        f"- 策略分布：{'、'.join(f'{k} {v}' for k, v in sorted(strategy_counts.items()))}",
        "",
        "## 你的高频话术风格",
        "",
    ]
    for message, count in self_messages.most_common(5):
        lines.append(f"- {count} 次：{message}")
    lines.extend(["", "## 生成原则", ""])
    lines.extend(f"- {rule}" for rule in STYLE_RULES)
    lines.extend(["", "## 策略规则", ""])
    lines.append("| 策略 | 阶段 | 样本 | 目标 | 模式 | 风险 | 升级方向 |")
    lines.append("|---|---|---:|---|---|---|---|")
    for rule in rules:
        lines.append(
            "| {key} | {stage} | {count} | {goal} | {pattern} | {risk} | {upgrade} |".format(
                key=rule["key"],
                stage=rule["stage"],
                count=rule["sample_count"],
                goal=rule["goal"].replace("|", "｜"),
                pattern=rule["pattern"].replace("|", "｜"),
                risk=rule["risk"].replace("|", "｜"),
                upgrade=rule["upgrade"].replace("|", "｜"),
            )
        )
    lines.extend(
        [
            "",
            "## 算法骨架",
            "",
            "1. 读候选人回复意图、项目置信度、是否已确认项目。",
            "2. 选择策略：问公司、正向匹配、要联系方式、薪资、拒绝、冷启动。",
            "3. 套用你的短句风格：承接原话 + 一句岗位信息 + 一个主问题或下一步动作。",
            "4. 输出只进入本地草稿，不自动发送。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build talk algorithm rules from Liepin samples.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = connect_db(db_path)
    try:
        ensure_table(conn)
        samples = load_samples(conn)
        rules = save_rules(conn, samples)
    finally:
        conn.close()
    report = write_report(samples, rules, output_dir)
    print(
        json.dumps(
            {
                "ok": True,
                "samples": len(samples),
                "rules": len(rules),
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
