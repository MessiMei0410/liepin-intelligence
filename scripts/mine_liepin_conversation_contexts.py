#!/usr/bin/env python3
"""Mine real Liepin conversation contexts into dialog-pair reply rules."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from reply_intelligence_rules import classify_reply, normalize_message


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


CONTEXT_RULE_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_context_reply_rules (
    rule_key TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    trigger_text TEXT NOT NULL,
    recommended_pattern TEXT NOT NULL,
    avoid_pattern TEXT,
    evidence_count INTEGER DEFAULT 0,
    confidence INTEGER DEFAULT 3,
    examples_json TEXT DEFAULT '[]',
    updated_at TEXT DEFAULT (datetime('now','localtime'))
)
"""


PAIR_BLUEPRINTS = {
    "positive_interest": {
        "category": "真实对话-正向兴趣",
        "trigger": "可以聊聊 / 感兴趣 / 希望详聊",
        "pattern": "先承接兴趣，再点明岗位锚点，优先约 10 分钟电话或转微信继续。",
        "avoid": "不要只回好的，也不要第一条就连续追问多个筛选条件。",
    },
    "asks_if_open": {
        "category": "真实对话-确认在招",
        "trigger": "还在招吗 / 目前还招吗",
        "pattern": "直接回答还在招，补岗位锚点，再只问一个关键匹配问题。",
        "avoid": "不要只回答在招而没有任何岗位信息或下一步。",
    },
    "location_probe": {
        "category": "真实对话-地点关注",
        "trigger": "有西安机会吗 / 地点在哪里 / 通勤远",
        "pattern": "先确认地点事实或限制，再短句说明是否匹配，不强推。",
        "avoid": "地点不合适时不要继续卖岗位亮点。",
    },
    "contact_exchange": {
        "category": "真实对话-转联系方式",
        "trigger": "微信 / 手机号 / 发简历",
        "pattern": "承接联系方式，补一句机会锚点，再说明到微信里发岗位要点。",
        "avoid": "不要只收联系方式，不说明为何要继续沟通。",
    },
    "asks_company_or_jd": {
        "category": "真实对话-要岗位信息",
        "trigger": "哪家公司 / 做什么方向 / JD",
        "pattern": "先给关键信息，再问一个年限或方向问题，不要反过来盘问。",
        "avoid": "不要绕开候选人的信息请求。",
    },
    "self_recommendation": {
        "category": "真实对话-候选人自荐",
        "trigger": "您看下合适吗 / 我这边经历匹配",
        "pattern": "先认可收到背景，再约个短沟通快速对齐岗位重点。",
        "avoid": "不要让候选人重复证明自己匹配。",
    },
    "graceful_close": {
        "category": "真实对话-不匹配收口",
        "trigger": "不合适 / 通勤远 / 暂不考虑",
        "pattern": "短句尊重反馈，必要时明确当前没有匹配机会，保留后续联系空间。",
        "avoid": "不要继续追问或改口强推。",
    },
    "unclear": {
        "category": "真实对话-待人工判断",
        "trigger": "意图不明确",
        "pattern": "优先人工复核，不自动套强推进话术。",
        "avoid": "不要在意图不清时直接外发强动作回复。",
    },
}


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(CONTEXT_RULE_SCHEMA)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversation_context_reply_rules_category ON conversation_context_reply_rules(category)"
    )
    conn.commit()


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def confidence(count: int) -> int:
    if count >= 12:
        return 5
    if count >= 6:
        return 4
    if count >= 2:
        return 3
    return 2 if count else 1


def resolve_context_files(output_dir: Path, explicit_file: str | None, merge_all: bool) -> list[Path]:
    if explicit_file:
        path = Path(explicit_file).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"未找到上下文文件：{path}")
        return [path]
    files = sorted(output_dir.glob("liepin_conversation_contexts_*.json"))
    if not files:
        raise FileNotFoundError("还没有可用的上下文采样文件，请先运行 collect_liepin_conversation_contexts.py")
    return files if merge_all else [files[-1]]


def bucket_for_pair(intent: str, text: str) -> str:
    normalized = normalize_message(text)
    if "还在招" in normalized or "目前还有" in normalized:
        return "asks_if_open"
    if intent == "need_contact" or any(word in normalized for word in ("微信", "手机号", "简历")):
        return "contact_exchange"
    if intent == "location_concern" or any(word in normalized for word in ("西安", "上海", "苏州", "通勤", "地点", "城市")):
        return "location_probe"
    if intent == "need_more_info" or any(word in normalized for word in ("公司", "方向", "岗位", "jd", "做什么")):
        return "asks_company_or_jd"
    if "您看下合适吗" in normalized or "看下合适吗" in normalized:
        return "self_recommendation"
    if intent == "not_interested":
        return "graceful_close"
    if intent == "interested":
        return "positive_interest"
    return "unclear"


def load_pairs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for convo in payload.get("conversations", []):
        messages = convo.get("messages") or []
        preview = convo.get("preview") or {}
        idx = 0
        while idx < len(messages):
            current = messages[idx]
            if current.get("direction_hint") != "candidate":
                idx += 1
                continue

            candidate_parts = [clean(current.get("text"))]
            idx += 1
            while idx < len(messages) and messages[idx].get("direction_hint") == "candidate":
                candidate_parts.append(clean(messages[idx].get("text")))
                idx += 1

            if idx >= len(messages) or messages[idx].get("direction_hint") != "self":
                continue

            reply_parts = [clean(messages[idx].get("text"))]
            idx += 1
            while idx < len(messages) and messages[idx].get("direction_hint") == "self":
                reply_parts.append(clean(messages[idx].get("text")))
                idx += 1

            candidate_text = " / ".join([part for part in candidate_parts if part])
            reply_text = " / ".join([part for part in reply_parts if part])
            if not candidate_text or not reply_text:
                continue
            classified = classify_reply(candidate_parts[-1] if candidate_parts else candidate_text)
            bucket = bucket_for_pair(classified.get("intent") or "", candidate_text)
            pairs.append(
                {
                    "candidate_name": clean(preview.get("name")),
                    "candidate_title": clean(preview.get("title")),
                    "candidate_text": candidate_text,
                    "reply_text": reply_text,
                    "intent": classified.get("intent") or "unclear",
                    "tags": classified.get("reply_tags") or [],
                    "bucket": bucket,
                }
            )
    return pairs


def merge_payloads(files: list[Path]) -> tuple[dict[str, Any], list[str]]:
    merged_conversations: list[dict[str, Any]] = []
    sources: list[str] = []
    seen_keys: set[str] = set()
    latest_time = ""
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        sources.append(str(path))
        latest_time = max(latest_time, clean(payload.get("collectedAt")))
        for convo in payload.get("conversations", []):
            preview = convo.get("preview") or {}
            key = "｜".join(
                [
                    clean(preview.get("name")),
                    clean(preview.get("title")),
                    clean(preview.get("timeText")),
                    clean(preview.get("message")),
                ]
            )
            if key and key not in seen_keys:
                seen_keys.add(key)
                merged_conversations.append(convo)
    return {
        "collectedAt": latest_time or datetime.now().isoformat(timespec="seconds"),
        "conversations": merged_conversations,
    }, sources


def example_text(rows: list[dict[str, Any]], limit: int = 3) -> list[str]:
    examples: list[str] = []
    for row in rows:
        text = f"候选人：{clean(row['candidate_text'])} -> 你的回复：{clean(row['reply_text'])}"
        if text not in examples:
            examples.append(text[:180] + ("..." if len(text) > 180 else ""))
        if len(examples) >= limit:
            break
    return examples


def build_rules(pairs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Counter]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    intents: Counter = Counter()
    for pair in pairs:
        buckets[pair["bucket"]].append(pair)
        intents[pair["intent"]] += 1
    rules: list[dict[str, Any]] = []
    for key, blueprint in PAIR_BLUEPRINTS.items():
        rows = buckets.get(key, [])
        if not rows and key == "unclear":
            continue
        rules.append(
            {
                "rule_key": f"context_{key}",
                "category": blueprint["category"],
                "trigger_text": blueprint["trigger"],
                "recommended_pattern": blueprint["pattern"],
                "avoid_pattern": blueprint["avoid"],
                "evidence_count": len(rows),
                "confidence": confidence(len(rows)),
                "examples": example_text(rows),
            }
        )
    return rules, intents


def save_rules(conn: sqlite3.Connection, rules: list[dict[str, Any]]) -> None:
    for rule in rules:
        conn.execute(
            """
            INSERT INTO conversation_context_reply_rules (
                rule_key, category, trigger_text, recommended_pattern, avoid_pattern,
                evidence_count, confidence, examples_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
            ON CONFLICT(rule_key) DO UPDATE SET
                category = excluded.category,
                trigger_text = excluded.trigger_text,
                recommended_pattern = excluded.recommended_pattern,
                avoid_pattern = excluded.avoid_pattern,
                evidence_count = excluded.evidence_count,
                confidence = excluded.confidence,
                examples_json = excluded.examples_json,
                updated_at = datetime('now','localtime')
            """,
            (
                rule["rule_key"],
                rule["category"],
                rule["trigger_text"],
                rule["recommended_pattern"],
                rule["avoid_pattern"],
                int(rule["evidence_count"]),
                int(rule["confidence"]),
                json.dumps(rule.get("examples") or [], ensure_ascii=False),
            ),
        )
    conn.commit()


def write_report(output_dir: Path, source_files: list[str], payload: dict[str, Any], pairs: list[dict[str, Any]], rules: list[dict[str, Any]], intents: Counter) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘真实对话学习_{stamp}.md"
    bucket_counts = Counter(pair["bucket"] for pair in pairs)
    lines = [
        "# 猎聘真实对话学习",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 来源文件数：{len(source_files)}",
        f"- 会话数：{len(payload.get('conversations', []))}",
        f"- 真实往返对数：{len(pairs)}",
        f"- 候选人意图分布：{'、'.join(f'{k} {v}' for k, v in intents.most_common()) or '暂无'}",
        "",
        "## 数据来源",
        "",
    ]
    lines.extend(f"- {source}" for source in source_files[:10])
    if len(source_files) > 10:
        lines.append(f"- 其余 {len(source_files) - 10} 个文件已合并")
    lines.extend(
        [
            "",
        "## 场景分布",
        "",
        "| 场景 | 数量 |",
        "|---|---:|",
        ]
    )
    for key, count in bucket_counts.most_common():
        lines.append(f"| {PAIR_BLUEPRINTS[key]['category']} | {count} |")
    lines.extend(
        [
            "",
            "## 学到的真实接话规则",
            "",
            "| 场景 | 证据 | 推荐写法 | 避免 |",
            "|---|---:|---|---|",
        ]
    )
    for rule in rules:
        lines.append(
            f"| {rule['category']} | {rule['evidence_count']} | {rule['recommended_pattern']} | {rule['avoid_pattern']} |"
        )
    lines.extend(
        [
            "",
            "## 真实对话样例",
            "",
        ]
    )
    if not pairs:
        lines.append("- 暂无可用的候选人一句 -> 你的下一句对话对。")
    else:
        for idx, pair in enumerate(pairs[:12], start=1):
            lines.append(f"{idx}. {pair['candidate_name'] or '未识别'}｜{pair['candidate_title'] or '未识别'}")
            lines.append(f"   - 候选人：{pair['candidate_text']}")
            lines.append(f"   - 你的回复：{pair['reply_text']}")
            lines.append(f"   - 归类：{PAIR_BLUEPRINTS[pair['bucket']]['category']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Mine Liepin conversation contexts into dialog-pair rules.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--input-json", default="")
    parser.add_argument("--merge-all", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser()
    source_files = resolve_context_files(output_dir, args.input_json or None, args.merge_all)
    payload, source_list = merge_payloads(source_files)
    pairs = load_pairs(payload)
    rules, intents = build_rules(pairs)

    conn = connect(Path(args.db).expanduser())
    try:
        ensure_schema(conn)
        save_rules(conn, rules)
    finally:
        conn.close()

    report = write_report(output_dir, source_list, payload, pairs, rules, intents)
    print(
        json.dumps(
            {
                "ok": True,
                "sources": source_list,
                "pairs": len(pairs),
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
