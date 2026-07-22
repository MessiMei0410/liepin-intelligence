#!/usr/bin/env python3
"""Sync the Liepin intelligence cache into a private Obsidian vault.

The private vault is the structured source of truth for recruiting data. The
SQLite database remains the execution cache for existing scripts. This exporter
keeps Markdown notes deterministic and updates only auto-sync blocks so manual
notes survive future refreshes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_PRIVATE_VAULT = Path.home() / "Documents" / "Obsidian Liepin Private Vault"
DEFAULT_PUBLIC_VAULT = Path.home() / "Documents" / "Obsidian Vault"
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"

AUTO_START = "<!-- AUTO_SYNC_START -->"
AUTO_END = "<!-- AUTO_SYNC_END -->"
MANUAL_SECTION = "## 人工补充\n\n- \n"
POSITIVE_FEEDBACK = {"approved", "interviewing", "interview_passed", "offer", "hired"}
NEGATIVE_FEEDBACK = {"rejected", "interview_failed", "eliminated"}
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)\b(cookie|authorization|bearer token|api[_ -]?key|password)\s*[:=]"),
]
SENSITIVE_REF_PATTERNS = [
    re.compile(r"https?://h\.liepin\.com/\S+"),
    re.compile(r"\b(?:res_id_encode|ck_id|sk_id|fk_id|skId|ckId|fkId)=[^&\s|]+"),
]


@dataclass
class WriteStats:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    planned_create: int = 0
    planned_update: int = 0
    skipped: int = 0
    files: list[str] = field(default_factory=list)

    def bump(self, key: str, path: Path) -> None:
        setattr(self, key, getattr(self, key) + 1)
        if key != "unchanged":
            self.files.append(str(path))


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def safe_rows(conn: sqlite3.Connection, table: str, order_by: str = "id") -> list[sqlite3.Row]:
    if not table_exists(conn, table):
        return []
    try:
        return conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}").fetchall()
    except sqlite3.OperationalError:
        return conn.execute(f"SELECT * FROM {table}").fetchall()


def row_get(row: sqlite3.Row | dict[str, Any] | None, key: str, default: Any = "") -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    return row[key] if key in row.keys() else default


def clean(value: Any) -> str:
    text = str(value or "")
    for pattern in SENSITIVE_REF_PATTERNS:
        text = pattern.sub("[猎聘链接已省略]", text)
    return " ".join(text.replace("\u3000", " ").split())


def short(value: Any, limit: int = 120) -> str:
    text = clean(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def parse_json(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return fallback


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def slugify(value: Any, fallback: str = "untitled", limit: int = 48) -> str:
    text = clean(value).lower()
    chars: list[str] = []
    for ch in text:
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            chars.append(ch)
        elif chars and chars[-1] != "-":
            chars.append("-")
    slug = "".join(chars).strip("-")
    if not slug:
        slug = fallback
    return slug[:limit].strip("-") or fallback


def obsidian_link(name: str, label: str | None = None) -> str:
    safe_name = name.replace("[", "").replace("]", "")
    if label and label != safe_name:
        return f"[[{safe_name}|{label}]]"
    return f"[[{safe_name}]]"


def candidate_key(name: Any, company: Any) -> tuple[str, str]:
    return (clean(name), clean(company))


def project_key(client: Any, position: Any) -> tuple[str, str]:
    return (clean(client), clean(position))


def project_label(client: Any, position: Any) -> str:
    c, p = project_key(client, position)
    if c and p:
        return f"{c}/{p}"
    if c:
        return f"{c}/未定岗位"
    if p:
        return f"未定客户/{p}"
    return "未定客户/未定岗位"


def note_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def sync_time() -> str:
    return datetime.now().isoformat(timespec="seconds")


def yaml_value(value: Any) -> str:
    text = str(value or "").replace('"', '\\"')
    return f'"{text}"'


def frontmatter(title: str, page_type: str, tags: list[str], extra: dict[str, Any] | None = None) -> str:
    lines = [
        "---",
        f"title: {yaml_value(title)}",
        f"created: {note_date()}",
        f"updated: {note_date()}",
        f"type: {page_type}",
        "tags: [" + ", ".join(tags) + "]",
    ]
    for key, value in (extra or {}).items():
        if isinstance(value, list):
            lines.append(f"{key}: [" + ", ".join(yaml_value(item) for item in value) + "]")
        else:
            lines.append(f"{key}: {yaml_value(value)}")
    lines.append("---")
    return "\n".join(lines)


def table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        cells = [str(cell if cell not in (None, "") else "未填").replace("\n", "<br>") for cell in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def list_items(values: Any) -> str:
    if isinstance(values, str):
        values = parse_json(values, [])
    if isinstance(values, dict):
        values = [f"{key}: {value}" for key, value in values.items()]
    if not values:
        return "- 暂无"
    return "\n".join(f"- {clean(item)}" for item in values if clean(item)) or "- 暂无"


def check_no_secrets(content: str, path: Path) -> None:
    for pattern in SECRET_PATTERNS:
        if pattern.search(content):
            raise ValueError(f"Refusing to write possible secret to {path}")


class VaultWriter:
    def __init__(self, vault: Path, dry_run: bool = False):
        self.vault = vault
        self.dry_run = dry_run
        self.stats = WriteStats()

    def ensure_dirs(self) -> None:
        dirs = [
            "00_Index",
            "10_Clients",
            "20_Roles",
            "30_Candidates",
            "40_Interactions",
            "50_Searches",
            "60_Reviews",
            "90_System",
        ]
        if self.dry_run:
            return
        for dirname in dirs:
            (self.vault / dirname).mkdir(parents=True, exist_ok=True)

    def write_note(self, relative: str, title: str, page_type: str, tags: list[str], auto_body: str, extra: dict[str, Any] | None = None) -> Path:
        path = self.vault / relative
        auto_block = f"{AUTO_START}\n{auto_body.rstrip()}\n{AUTO_END}"
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if AUTO_START in existing and AUTO_END in existing:
                prefix, rest = existing.split(AUTO_START, 1)
                _, suffix = rest.split(AUTO_END, 1)
                content = prefix.rstrip() + "\n\n" + auto_block + suffix
            else:
                content = existing.rstrip() + "\n\n" + auto_block + "\n"
        else:
            content = (
                frontmatter(title, page_type, tags, extra)
                + "\n\n"
                + f"# {title}\n\n"
                + auto_block
                + "\n\n"
                + MANUAL_SECTION
            )
        check_no_secrets(content, path)
        existed = path.exists()
        old = path.read_text(encoding="utf-8") if existed else None
        if old == content:
            self.stats.bump("unchanged", path)
            return path
        if self.dry_run:
            self.stats.bump("planned_update" if existed else "planned_create", path)
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.stats.bump("updated" if existed else "created", path)
        return path


class LiepinObsidianSync:
    def __init__(self, conn: sqlite3.Connection, private_vault: Path, public_vault: Path, output_dir: Path, dry_run: bool = False):
        self.conn = conn
        self.private_vault = private_vault
        self.public_vault = public_vault
        self.output_dir = output_dir
        self.dry_run = dry_run
        self.writer = VaultWriter(private_vault, dry_run=dry_run)
        self.now = sync_time()
        self.note_by_candidate_id: dict[int, str] = {}
        self.note_by_candidate_key: dict[tuple[str, str], str] = {}
        self.client_notes: dict[str, str] = {}
        self.role_notes: dict[tuple[str, str], str] = {}
        self.event_notes_by_candidate: dict[str, list[str]] = defaultdict(list)
        self.search_notes_by_project: dict[tuple[str, str], list[str]] = defaultdict(list)

    def load_data(self) -> dict[str, list[sqlite3.Row]]:
        return {
            table_name: safe_rows(self.conn, table_name)
            for table_name in [
                "candidates",
                "candidate_profiles",
                "candidate_intelligence",
                "candidate_replies",
                "followup_tasks",
                "outreach_events",
                "client_feedback_events",
                "search_experiments",
                "positions",
                "position_profiles",
                "learning_notes",
                "reply_learning_rules",
                "strategy_corrections",
            ]
        }

    def candidate_note_name(self, candidate_id: Any, name: Any, company: Any) -> str:
        if candidate_id not in (None, ""):
            return f"candidate-{candidate_id}-{slugify(name or company, 'candidate')}"
        digest = hashlib.sha1(f"{clean(name)}|{clean(company)}".encode("utf-8")).hexdigest()[:10]
        return f"candidate-unmatched-{digest}-{slugify(name or company, 'candidate')}"

    def role_note_name(self, client: Any, position: Any) -> str:
        label = project_label(client, position).replace("/", "-")
        return slugify(label, "role", limit=80)

    def prepare_note_names(self, data: dict[str, list[sqlite3.Row]]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        seen_keys: set[tuple[str, str]] = set()
        for row in data["candidates"]:
            candidate_id = int(row_get(row, "id"))
            name = clean(row_get(row, "name"))
            company = clean(row_get(row, "company"))
            note_name = self.candidate_note_name(candidate_id, name, company)
            self.note_by_candidate_id[candidate_id] = note_name
            if name or company:
                self.note_by_candidate_key[candidate_key(name, company)] = note_name
                seen_keys.add(candidate_key(name, company))
            seen_ids.add(candidate_id)
            candidates.append({"row": row, "virtual": False, "id": candidate_id, "note": note_name})

        source_tables = [
            "candidate_profiles",
            "candidate_intelligence",
            "candidate_replies",
            "followup_tasks",
            "outreach_events",
            "client_feedback_events",
        ]
        for table_name in source_tables:
            for row in data[table_name]:
                candidate_id = row_get(row, "candidate_id")
                try:
                    numeric_id = int(candidate_id) if candidate_id not in (None, "") else None
                except ValueError:
                    numeric_id = None
                if numeric_id and numeric_id in seen_ids:
                    continue
                name = clean(row_get(row, "candidate_name"))
                company = clean(row_get(row, "candidate_company"))
                key = candidate_key(name, company)
                if key in seen_keys or not (name or company):
                    continue
                note_name = self.candidate_note_name(None, name, company)
                self.note_by_candidate_key[key] = note_name
                seen_keys.add(key)
                candidates.append(
                    {
                        "row": {
                            "id": "",
                            "name": name,
                            "company": company,
                            "title": "",
                            "education": "",
                            "experience": "",
                            "skills": "",
                            "level": "",
                            "city": "",
                            "client": row_get(row, "client"),
                            "position": row_get(row, "position"),
                            "status": "from_interaction",
                            "source": table_name,
                            "created_at": row_get(row, "created_at"),
                            "updated_at": row_get(row, "updated_at"),
                        },
                        "virtual": True,
                        "id": None,
                        "note": note_name,
                    }
                )
        return candidates

    def candidate_link_for_row(self, row: sqlite3.Row | dict[str, Any]) -> str:
        candidate_id = row_get(row, "candidate_id")
        try:
            numeric_id = int(candidate_id) if candidate_id not in (None, "") else None
        except ValueError:
            numeric_id = None
        if numeric_id and numeric_id in self.note_by_candidate_id:
            note = self.note_by_candidate_id[numeric_id]
            return obsidian_link(note, clean(row_get(row, "candidate_name")) or note)
        key = candidate_key(row_get(row, "candidate_name"), row_get(row, "candidate_company"))
        note = self.note_by_candidate_key.get(key)
        if note:
            return obsidian_link(note, clean(row_get(row, "candidate_name")) or note)
        return clean(row_get(row, "candidate_name")) or "未定人选"

    def role_link(self, client: Any, position: Any) -> str:
        key = project_key(client, position)
        note = self.role_notes.get(key)
        if note:
            return obsidian_link(note, project_label(client, position))
        if clean(client) or clean(position):
            return project_label(client, position)
        return "未定项目"

    def build_indexes(self, data: dict[str, list[sqlite3.Row]], candidate_items: list[dict[str, Any]]) -> None:
        clients = sorted(
            {
                clean(row_get(row, "client"))
                for table_name in ["candidates", "positions", "position_profiles", "candidate_intelligence", "search_experiments"]
                for row in data[table_name]
                if clean(row_get(row, "client"))
            }
        )
        for client in clients:
            self.client_notes[client] = slugify(client, "client", 80)

        projects = sorted(
            {
                project_key(row_get(row, "client"), row_get(row, "position") or row_get(row, "title"))
                for table_name in ["positions", "position_profiles", "candidate_intelligence", "search_experiments", "followup_tasks"]
                for row in data[table_name]
                if clean(row_get(row, "client")) or clean(row_get(row, "position") or row_get(row, "title"))
            }
        )
        for key in projects:
            self.role_notes[key] = self.role_note_name(*key)

        candidate_lines = []
        for item in candidate_items:
            row = item["row"]
            candidate_lines.append(
                [
                    obsidian_link(item["note"], clean(row_get(row, "name")) or "未命名"),
                    clean(row_get(row, "company")),
                    clean(row_get(row, "title")),
                    self.role_link(row_get(row, "client"), row_get(row, "position")),
                    clean(row_get(row, "status")),
                ]
            )
        body = "\n".join(
            [
                f"同步时间：{self.now}",
                "",
                f"- 人选页：{len(candidate_items)}",
                f"- 客户页：{len(clients)}",
                f"- 岗位页：{len(projects)}",
                "",
                "## 人选索引",
                "",
                table(["人选", "当前公司", "当前职位", "项目", "状态"], candidate_lines[:1000]),
            ]
        )
        self.writer.write_note("00_Index/人选索引.md", "人选索引", "index", ["liepin", "candidate", "private"], body)

        self.writer.write_note(
            "00_Index/客户索引.md",
            "客户索引",
            "index",
            ["liepin", "client", "private"],
            "\n".join(f"- {obsidian_link(self.client_notes[client], client)}" for client in clients) or "- 暂无",
        )
        self.writer.write_note(
            "00_Index/岗位索引.md",
            "岗位索引",
            "index",
            ["liepin", "role", "private"],
            "\n".join(
                f"- {obsidian_link(note, project_label(*key))}"
                for key, note in sorted(self.role_notes.items(), key=lambda item: project_label(*item[0]))
            )
            or "- 暂无",
        )

    def write_system_pages(self) -> None:
        schema = f"""
同步时间：{self.now}

## 主库角色

- 本库是猎聘智能项目的私密 Obsidian 主库。
- `/Users/messi/.hermes/talent_pool.db` 继续作为执行缓存和兼容层。
- 现有公开知识库 `/Users/messi/Documents/Obsidian Vault` 只接收脱敏摘要。

## 页面类型

- `candidate`：一人一页，保存结构化全量资料和项目推进状态。
- `interaction`：触达、回复、待办、客户反馈事件。
- `search`：搜索实验、关键词、渠道效果。
- `client`：客户维度聚合。
- `role`：岗位维度聚合。
- `review`：项目复盘、规则、同步日志。

## 数据边界

- 本库可保存实名人选结构化数据。
- 默认不复制聊天全文、简历全文、联系方式全文。
- 猎聘 URL、Cookie、API key、token、账号密码、代理订阅 URL 不进入本库。
"""
        self.writer.write_note("90_System/SCHEMA.md", "猎聘私密主库 Schema", "system", ["liepin", "schema", "private"], schema)
        rules = """
## 允许进入私密库

- 人选基础字段、项目归属、匹配评分、风险点、验证问题、状态和下一步。
- 触达、回复、待办、客户反馈的结构化记录。
- 搜索实验、关键词、筛选条件和转化数字。

## 默认不写入

- 手机号、微信号、邮箱、身份证、住址。
- 完整聊天原文、简历正文、薪资截图、Offer 截图。
- API key、token、Cookie、账号密码、代理订阅 URL。

## 写入原则

结构化字段优先，原文通过来源行 ID 或本地附件路径追溯。需要公开沉淀时，只把脱敏结论同步到公开知识库。
"""
        self.writer.write_note("90_System/敏感信息规则.md", "猎聘私密库敏感信息规则", "system", ["liepin", "privacy", "private"], rules)

    def write_candidate_pages(self, data: dict[str, list[sqlite3.Row]], candidate_items: list[dict[str, Any]]) -> None:
        profiles_by_id = {int(row["candidate_id"]): row for row in data["candidate_profiles"] if row_get(row, "candidate_id") not in (None, "")}
        intelligence_by_id: dict[int, list[sqlite3.Row]] = defaultdict(list)
        intelligence_by_key: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        replies_by_id: dict[int, list[sqlite3.Row]] = defaultdict(list)
        replies_by_key: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        tasks_by_id: dict[int, list[sqlite3.Row]] = defaultdict(list)
        tasks_by_key: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        outreach_by_id: dict[int, list[sqlite3.Row]] = defaultdict(list)
        outreach_by_key: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        feedback_by_id: dict[int, list[sqlite3.Row]] = defaultdict(list)
        feedback_by_key: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)

        def add_by_id_and_key(row: sqlite3.Row, by_id: dict[int, list[sqlite3.Row]], by_key: dict[tuple[str, str], list[sqlite3.Row]]) -> None:
            candidate_id = row_get(row, "candidate_id")
            try:
                numeric_id = int(candidate_id) if candidate_id not in (None, "") else None
            except ValueError:
                numeric_id = None
            if numeric_id:
                by_id[numeric_id].append(row)
            key = candidate_key(row_get(row, "candidate_name"), row_get(row, "candidate_company"))
            if key != ("", ""):
                by_key[key].append(row)

        for row in data["candidate_intelligence"]:
            add_by_id_and_key(row, intelligence_by_id, intelligence_by_key)
        for row in data["candidate_replies"]:
            add_by_id_and_key(row, replies_by_id, replies_by_key)
        for row in data["followup_tasks"]:
            add_by_id_and_key(row, tasks_by_id, tasks_by_key)
        for row in data["outreach_events"]:
            add_by_id_and_key(row, outreach_by_id, outreach_by_key)
        for row in data["client_feedback_events"]:
            add_by_id_and_key(row, feedback_by_id, feedback_by_key)

        for item in candidate_items:
            row = item["row"]
            candidate_id = item["id"]
            key = candidate_key(row_get(row, "name"), row_get(row, "company"))
            profile = profiles_by_id.get(candidate_id) if candidate_id else None
            intelligence = (intelligence_by_id.get(candidate_id, []) if candidate_id else []) + intelligence_by_key.get(key, [])
            replies = (replies_by_id.get(candidate_id, []) if candidate_id else []) + replies_by_key.get(key, [])
            tasks = (tasks_by_id.get(candidate_id, []) if candidate_id else []) + tasks_by_key.get(key, [])
            outreach = (outreach_by_id.get(candidate_id, []) if candidate_id else []) + outreach_by_key.get(key, [])
            feedback = (feedback_by_id.get(candidate_id, []) if candidate_id else []) + feedback_by_key.get(key, [])

            latest_reply = max(replies, key=lambda r: clean(row_get(r, "message_time") or row_get(r, "created_at")), default=None)
            latest_outreach = max(outreach, key=lambda r: clean(row_get(r, "event_time") or row_get(r, "created_at")), default=None)
            open_tasks = [task for task in tasks if clean(row_get(task, "status") or "open") == "open"]
            title = clean(row_get(row, "name")) or clean(row_get(row, "company")) or item["note"]
            candidate_link = obsidian_link(item["note"], title)

            for event_row, prefix in [(reply, "reply") for reply in replies] + [(task, "task") for task in tasks] + [(event, "outreach") for event in outreach] + [(fb, "feedback") for fb in feedback]:
                event_note = f"{prefix}-{row_get(event_row, 'id')}-{slugify(title, prefix)}"
                self.event_notes_by_candidate[item["note"]].append(event_note)

            intelligence_rows = []
            for record in intelligence:
                intelligence_rows.append(
                    [
                        self.role_link(row_get(record, "client"), row_get(record, "position")),
                        row_get(record, "fit_score"),
                        clean(row_get(record, "fit_level")),
                        clean(row_get(record, "recommendation_decision")),
                        short(row_get(record, "next_action"), 80),
                    ]
                )
            task_rows = [
                [
                    obsidian_link(f"task-{row_get(task, 'id')}-{slugify(title, 'task')}", f"task-{row_get(task, 'id')}"),
                    clean(row_get(task, "task_type")),
                    row_get(task, "priority"),
                    clean(row_get(task, "status")),
                    short(row_get(task, "reason"), 80),
                ]
                for task in tasks[:20]
            ]
            reply_rows = [
                [
                    obsidian_link(f"reply-{row_get(reply, 'id')}-{slugify(title, 'reply')}", f"reply-{row_get(reply, 'id')}"),
                    clean(row_get(reply, "intent")),
                    clean(row_get(reply, "sentiment")),
                    clean(row_get(reply, "match_confidence")),
                    len(clean(row_get(reply, "raw_text"))),
                    short(row_get(reply, "suggested_next_action"), 80),
                ]
                for reply in replies[:20]
            ]
            outreach_rows = [
                [
                    obsidian_link(f"outreach-{row_get(event, 'id')}-{slugify(title, 'outreach')}", f"outreach-{row_get(event, 'id')}"),
                    clean(row_get(event, "event_type")),
                    clean(row_get(event, "event_status")),
                    clean(row_get(event, "event_time")),
                    short(row_get(event, "message_summary"), 80),
                ]
                for event in outreach[:20]
            ]
            feedback_rows = [
                [
                    obsidian_link(f"feedback-{row_get(event, 'id')}-{slugify(title, 'feedback')}", f"feedback-{row_get(event, 'id')}"),
                    clean(row_get(event, "feedback_type")),
                    clean(row_get(event, "status_after")),
                    short(row_get(event, "next_action"), 80),
                ]
                for event in feedback[:20]
            ]

            blocks = [
                f"同步时间：{self.now}",
                "",
                "## 基础字段",
                "",
                table(
                    ["字段", "值"],
                    [
                        ["SQLite candidate id", candidate_id or "虚拟人选"],
                        ["姓名", title],
                        ["当前公司", clean(row_get(row, "company"))],
                        ["当前职位", clean(row_get(row, "title"))],
                        ["学历", clean(row_get(row, "education"))],
                        ["经验", clean(row_get(row, "experience"))],
                        ["城市", clean(row_get(row, "city"))],
                        ["层级", clean(row_get(row, "level"))],
                        ["技能", short(row_get(row, "skills"), 240)],
                        ["标签", clean(row_get(row, "talent_pool"))],
                        ["来源", clean(row_get(row, "source"))],
                        ["外部候选人 ID", clean(row_get(row, "xsaas_id"))],
                        ["状态", clean(row_get(row, "status"))],
                    ],
                ),
                "",
                "## 项目字段",
                "",
                table(
                    ["字段", "值"],
                    [
                        ["客户", clean(row_get(row, "client"))],
                        ["岗位", clean(row_get(row, "position"))],
                        ["项目链接", self.role_link(row_get(row, "client"), row_get(row, "position"))],
                        ["资料摘要", clean(row_get(profile, "profile_summary")) if profile else "暂无"],
                        ["教育层级", clean(row_get(profile, "education_level")) if profile else "暂无"],
                        ["资深度", clean(row_get(profile, "seniority")) if profile else "暂无"],
                        ["行业标签", ", ".join(parse_json(row_get(profile, "industry_tags_json"), [])) if profile else "暂无"],
                        ["职能标签", ", ".join(parse_json(row_get(profile, "function_tags_json"), [])) if profile else "暂无"],
                        ["风险标签", ", ".join(parse_json(row_get(profile, "risk_tags_json"), [])) if profile else "暂无"],
                    ],
                ),
                "",
                "## 人岗匹配",
                "",
                table(["项目", "分数", "等级", "推荐判断", "下一步"], intelligence_rows) if intelligence_rows else "- 暂无",
            ]
            if intelligence:
                blocks.extend(["", "### 匹配证据与验证问题", ""])
                for record in intelligence[:10]:
                    blocks.extend(
                        [
                            f"#### {project_label(row_get(record, 'client'), row_get(record, 'position'))}",
                            "",
                            "**强匹配点**",
                            "",
                            list_items(row_get(record, "strong_matches_json")),
                            "",
                            "**弱匹配/待确认**",
                            "",
                            list_items(row_get(record, "weak_matches_json")),
                            "",
                            "**建议追问**",
                            "",
                            list_items(row_get(record, "verification_questions_json")),
                            "",
                        ]
                    )
            blocks.extend(
                [
                    "## 运营字段",
                    "",
                    table(
                        ["字段", "值"],
                        [
                            ["最近触达", f"{clean(row_get(latest_outreach, 'event_time'))} {clean(row_get(latest_outreach, 'event_type'))}" if latest_outreach else "暂无"],
                            ["最近回复", f"{clean(row_get(latest_reply, 'message_time'))} {clean(row_get(latest_reply, 'intent'))}" if latest_reply else "暂无"],
                            ["打开待办", len(open_tasks)],
                            ["触达事件", len(outreach)],
                            ["回复记录", len(replies)],
                            ["客户反馈", len(feedback)],
                        ],
                    ),
                    "",
                    "### 待办",
                    "",
                    table(["事件", "类型", "优先级", "状态", "原因"], task_rows) if task_rows else "- 暂无",
                    "",
                    "### 回复记录",
                    "",
                    "说明：不复制聊天全文，只保存意图、情绪、匹配和文本长度。",
                    "",
                    table(["事件", "意图", "情绪", "项目置信", "原文字数", "建议动作"], reply_rows) if reply_rows else "- 暂无",
                    "",
                    "### 触达记录",
                    "",
                    table(["事件", "类型", "状态", "时间", "摘要"], outreach_rows) if outreach_rows else "- 暂无",
                    "",
                    "### 客户反馈",
                    "",
                    table(["事件", "反馈类型", "后续状态", "下一步"], feedback_rows) if feedback_rows else "- 暂无",
                    "",
                    "## 来源字段",
                    "",
                    table(
                        ["字段", "值"],
                        [
                            ["源数据库", str(DEFAULT_DB)],
                            ["候选人原始行", candidate_id or "来自交互表"],
                            ["原始备注", short(row_get(row, "notes"), 240)],
                            ["创建时间", clean(row_get(row, "created_at"))],
                            ["更新时间", clean(row_get(row, "updated_at"))],
                        ],
                    ),
                ]
            )
            self.writer.write_note(
                f"30_Candidates/{item['note']}.md",
                title,
                "candidate",
                ["liepin", "candidate", "private"],
                "\n".join(blocks),
                {"candidate_id": candidate_id or "", "source": row_get(row, "source")},
            )

    def write_interaction_pages(self, data: dict[str, list[sqlite3.Row]]) -> None:
        for reply in data["candidate_replies"]:
            title = f"回复 {row_get(reply, 'id')} - {clean(row_get(reply, 'candidate_name')) or '未定人选'}"
            body = "\n".join(
                [
                    f"同步时间：{self.now}",
                    "",
                    table(
                        ["字段", "值"],
                        [
                            ["人选", self.candidate_link_for_row(reply)],
                            ["项目", self.role_link(row_get(reply, "client"), row_get(reply, "position"))],
                            ["渠道", clean(row_get(reply, "channel"))],
                            ["消息时间", clean(row_get(reply, "message_time"))],
                            ["方向", clean(row_get(reply, "direction"))],
                            ["意图", clean(row_get(reply, "intent"))],
                            ["情绪", clean(row_get(reply, "sentiment"))],
                            ["阻力标签", ", ".join(parse_json(row_get(reply, "blockers_json"), []))],
                            ["建议动作", short(row_get(reply, "suggested_next_action"), 200)],
                            ["项目置信", clean(row_get(reply, "match_confidence"))],
                            ["匹配原因", short(row_get(reply, "match_reason"), 200)],
                            ["话术策略", clean(row_get(reply, "talk_strategy"))],
                            ["原文字数", len(clean(row_get(reply, "raw_text")))],
                        ],
                    ),
                    "",
                    "原文未复制进 Obsidian；需要追溯时查看 SQLite `candidate_replies` 对应行。",
                ]
            )
            self.writer.write_note(
                f"40_Interactions/reply-{row_get(reply, 'id')}-{slugify(row_get(reply, 'candidate_name'), 'reply')}.md",
                title,
                "interaction",
                ["liepin", "reply", "private"],
                body,
            )

        for task in data["followup_tasks"]:
            title = f"待办 {row_get(task, 'id')} - {clean(row_get(task, 'candidate_name')) or '未定人选'}"
            body = "\n".join(
                [
                    f"同步时间：{self.now}",
                    "",
                    table(
                        ["字段", "值"],
                        [
                            ["人选", self.candidate_link_for_row(task)],
                            ["项目", self.role_link(row_get(task, "client"), row_get(task, "position"))],
                            ["类型", clean(row_get(task, "task_type"))],
                            ["优先级", row_get(task, "priority")],
                            ["状态", clean(row_get(task, "status"))],
                            ["到期", clean(row_get(task, "due_at"))],
                            ["原因", short(row_get(task, "reason"), 240)],
                            ["项目置信", clean(row_get(task, "match_confidence"))],
                            ["话术策略", clean(row_get(task, "talk_strategy"))],
                            ["分层", clean(row_get(task, "lane_tag"))],
                            ["关闭时间", clean(row_get(task, "closed_at"))],
                            ["关闭说明", short(row_get(task, "resolution_note"), 200)],
                        ],
                    ),
                    "",
                    "草稿话术未全文复制；需要追溯时查看 SQLite `followup_tasks` 对应行。",
                ]
            )
            self.writer.write_note(
                f"40_Interactions/task-{row_get(task, 'id')}-{slugify(row_get(task, 'candidate_name'), 'task')}.md",
                title,
                "interaction",
                ["liepin", "task", "private"],
                body,
            )

        for event in data["outreach_events"]:
            title = f"触达 {row_get(event, 'id')} - {clean(row_get(event, 'candidate_name')) or '未定人选'}"
            body = "\n".join(
                [
                    f"同步时间：{self.now}",
                    "",
                    table(
                        ["字段", "值"],
                        [
                            ["人选", self.candidate_link_for_row(event)],
                            ["项目", self.role_link(row_get(event, "client"), row_get(event, "position"))],
                            ["渠道", clean(row_get(event, "channel"))],
                            ["类型", clean(row_get(event, "event_type"))],
                            ["状态", clean(row_get(event, "event_status"))],
                            ["时间", clean(row_get(event, "event_time"))],
                            ["摘要", short(row_get(event, "message_summary"), 240)],
                            ["来源 URL", "有，未复制" if clean(row_get(event, "source_url")) else "无"],
                        ],
                    ),
                ]
            )
            self.writer.write_note(
                f"40_Interactions/outreach-{row_get(event, 'id')}-{slugify(row_get(event, 'candidate_name'), 'outreach')}.md",
                title,
                "interaction",
                ["liepin", "outreach", "private"],
                body,
            )

        for feedback in data["client_feedback_events"]:
            title = f"客户反馈 {row_get(feedback, 'id')} - {clean(row_get(feedback, 'candidate_name')) or '未定人选'}"
            body = "\n".join(
                [
                    f"同步时间：{self.now}",
                    "",
                    table(
                        ["字段", "值"],
                        [
                            ["人选", self.candidate_link_for_row(feedback)],
                            ["项目", self.role_link(row_get(feedback, "client"), row_get(feedback, "position"))],
                            ["反馈类型", clean(row_get(feedback, "feedback_type"))],
                            ["后续状态", clean(row_get(feedback, "status_after"))],
                            ["原因标签", ", ".join(parse_json(row_get(feedback, "reason_tags_json"), []))],
                            ["反馈摘要", short(row_get(feedback, "feedback_detail"), 300)],
                            ["下一步", short(row_get(feedback, "next_action"), 200)],
                            ["时间", clean(row_get(feedback, "feedback_time"))],
                        ],
                    ),
                ]
            )
            self.writer.write_note(
                f"40_Interactions/feedback-{row_get(feedback, 'id')}-{slugify(row_get(feedback, 'candidate_name'), 'feedback')}.md",
                title,
                "interaction",
                ["liepin", "feedback", "private"],
                body,
            )

    def write_search_pages(self, data: dict[str, list[sqlite3.Row]]) -> None:
        for row in data["search_experiments"]:
            key = project_key(row_get(row, "client"), row_get(row, "position"))
            note_name = f"search-{row_get(row, 'id')}-{slugify(project_label(*key) + '-' + clean(row_get(row, 'query')), 'search', 90)}"
            self.search_notes_by_project[key].append(note_name)
            title = f"搜索实验 {row_get(row, 'id')} - {project_label(*key)}"
            body = "\n".join(
                [
                    f"同步时间：{self.now}",
                    "",
                    table(
                        ["字段", "值"],
                        [
                            ["项目", self.role_link(*key)],
                            ["渠道", clean(row_get(row, "channel"))],
                            ["轮次", clean(row_get(row, "round_name"))],
                            ["关键词", clean(row_get(row, "query"))],
                            ["筛选条件", json_text(parse_json(row_get(row, "filters_json"), {}))],
                            ["结果数", row_get(row, "result_count")],
                            ["查看数", row_get(row, "viewed_count")],
                            ["入库数", row_get(row, "extracted_count")],
                            ["推荐数", row_get(row, "recommended_count")],
                            ["回复数", row_get(row, "reply_count")],
                            ["正向回复", row_get(row, "positive_reply_count")],
                            ["状态", clean(row_get(row, "status"))],
                            ["噪音备注", short(row_get(row, "noise_notes"), 300)],
                            ["来源 URL", "有，未复制" if clean(row_get(row, "source_url")) else "无"],
                        ],
                    ),
                ]
            )
            self.writer.write_note(f"50_Searches/{note_name}.md", title, "search", ["liepin", "search", "private"], body)

    def write_client_and_role_pages(self, data: dict[str, list[sqlite3.Row]]) -> None:
        candidate_counts = Counter(clean(row_get(row, "client")) for row in data["candidates"] if clean(row_get(row, "client")))
        position_counts = Counter(clean(row_get(row, "client")) for row in data["positions"] if clean(row_get(row, "client")))
        reply_counts = Counter(clean(row_get(row, "client")) for row in data["candidate_replies"] if clean(row_get(row, "client")))
        task_counts = Counter(clean(row_get(row, "client")) for row in data["followup_tasks"] if clean(row_get(row, "client")))
        search_counts = Counter(clean(row_get(row, "client")) for row in data["search_experiments"] if clean(row_get(row, "client")))

        for client, note_name in self.client_notes.items():
            roles = [
                self.role_link(c, p)
                for c, p in self.role_notes
                if c == client
            ]
            body = "\n".join(
                [
                    f"同步时间：{self.now}",
                    "",
                    table(
                        ["指标", "数量"],
                        [
                            ["候选人", candidate_counts[client]],
                            ["岗位记录", position_counts[client]],
                            ["回复", reply_counts[client]],
                            ["打开/历史待办", task_counts[client]],
                            ["搜索实验", search_counts[client]],
                        ],
                    ),
                    "",
                    "## 关联岗位",
                    "",
                    "\n".join(f"- {role}" for role in sorted(set(roles))) or "- 暂无",
                ]
            )
            self.writer.write_note(f"10_Clients/{note_name}.md", client, "client", ["liepin", "client", "private"], body)

        profiles_by_project = {project_key(row_get(row, "client"), row_get(row, "position")): row for row in data["position_profiles"]}
        candidates_by_project = Counter(project_key(row_get(row, "client"), row_get(row, "position")) for row in data["candidates"])
        intelligence_by_project = Counter(project_key(row_get(row, "client"), row_get(row, "position")) for row in data["candidate_intelligence"])
        tasks_by_project = Counter(project_key(row_get(row, "client"), row_get(row, "position")) for row in data["followup_tasks"])
        searches_by_project = Counter(project_key(row_get(row, "client"), row_get(row, "position")) for row in data["search_experiments"])

        for key, note_name in self.role_notes.items():
            client, position = key
            profile = profiles_by_project.get(key)
            related_candidates = [
                obsidian_link(self.note_by_candidate_id[int(row_get(row, "id"))], clean(row_get(row, "name")))
                for row in data["candidates"]
                if project_key(row_get(row, "client"), row_get(row, "position")) == key and int(row_get(row, "id")) in self.note_by_candidate_id
            ][:50]
            search_links = [obsidian_link(note, note) for note in self.search_notes_by_project.get(key, [])[:30]]
            body = "\n".join(
                [
                    f"同步时间：{self.now}",
                    "",
                    "## 项目概览",
                    "",
                    table(
                        ["指标", "数量"],
                        [
                            ["候选人", candidates_by_project[key]],
                            ["智能画像", intelligence_by_project[key]],
                            ["待办", tasks_by_project[key]],
                            ["搜索实验", searches_by_project[key]],
                        ],
                    ),
                    "",
                    "## 岗位画像",
                    "",
                    table(
                        ["字段", "值"],
                        [
                            ["客户", obsidian_link(self.client_notes.get(client, slugify(client, 'client')), client) if client else "未定客户"],
                            ["岗位", position or "未定岗位"],
                            ["学历要求", clean(row_get(profile, "education_requirement"))],
                            ["经验要求", clean(row_get(profile, "experience_requirement"))],
                            ["JD 摘要", clean(row_get(profile, "jd_analysis_summary"))],
                        ],
                    ),
                    "",
                    "### 硬性门槛",
                    "",
                    list_items(row_get(profile, "hard_requirements_json")),
                    "",
                    "### 软性偏好",
                    "",
                    list_items(row_get(profile, "soft_preferences_json")),
                    "",
                    "### 能力关键词",
                    "",
                    list_items(row_get(profile, "ability_keywords_json")),
                    "",
                    "### 搜索关键词",
                    "",
                    list_items(row_get(profile, "search_keywords_json")),
                    "",
                    "### 目标公司",
                    "",
                    list_items(row_get(profile, "target_companies_json")),
                    "",
                    "## 关联人选",
                    "",
                    "\n".join(f"- {item}" for item in related_candidates) or "- 暂无",
                    "",
                    "## 搜索实验",
                    "",
                    "\n".join(f"- {item}" for item in search_links) or "- 暂无",
                ]
            )
            self.writer.write_note(f"20_Roles/{note_name}.md", project_label(*key), "role", ["liepin", "role", "private"], body)

    def write_reviews(self, data: dict[str, list[sqlite3.Row]]) -> None:
        learning_rows = [
            [
                self.role_link(row_get(row, "client"), row_get(row, "position")),
                clean(row_get(row, "topic")),
                short(row_get(row, "note"), 200),
                row_get(row, "confidence"),
            ]
            for row in data["learning_notes"]
        ]
        rules_rows = [
            [
                clean(row_get(row, "category")),
                clean(row_get(row, "rule_key")),
                short(row_get(row, "rule_text"), 220),
                row_get(row, "evidence_count"),
                row_get(row, "confidence"),
            ]
            for row in data["reply_learning_rules"]
        ]
        strategy_rows = [
            [
                self.role_link(row_get(row, "client"), row_get(row, "position")),
                ", ".join(parse_json(row_get(row, "promote_keywords_json"), [])),
                ", ".join(parse_json(row_get(row, "suppress_keywords_json"), [])),
                ", ".join(parse_json(row_get(row, "target_tags_json"), [])),
                ", ".join(parse_json(row_get(row, "blocker_tags_json"), [])),
            ]
            for row in data["strategy_corrections"]
        ]
        body = "\n".join(
            [
                f"同步时间：{self.now}",
                "",
                "## 学习笔记",
                "",
                table(["项目", "主题", "笔记", "置信"], learning_rows) if learning_rows else "- 暂无",
                "",
                "## 回复学习规则",
                "",
                table(["分类", "规则", "内容", "证据数", "置信"], rules_rows) if rules_rows else "- 暂无",
                "",
                "## 策略修正规则",
                "",
                table(["项目", "保留关键词", "降权关键词", "目标标签", "阻力标签"], strategy_rows) if strategy_rows else "- 暂无",
            ]
        )
        self.writer.write_note("60_Reviews/猎聘智能规则沉淀.md", "猎聘智能规则沉淀", "review", ["liepin", "review", "private"], body)

    def write_home(self, data: dict[str, list[sqlite3.Row]], candidate_items: list[dict[str, Any]]) -> None:
        counts = {name: len(rows) for name, rows in data.items()}
        body = "\n".join(
            [
                f"同步时间：{self.now}",
                "",
                "## 今日入口",
                "",
                "- [[日常操作台]]",
                "- [[每日拍板摘要]]",
                "- [[确认后执行手册]]",
                "- [[待确认清单]]",
                "- [[人选索引]]",
                "- [[客户索引]]",
                "- [[岗位索引]]",
                "- [[猎聘智能规则沉淀]]",
                "- [[同步日志]]",
                "",
                "## 数据概览",
                "",
                table(
                    ["数据项", "数量"],
                    [
                        ["人选页", len(candidate_items)],
                        ["候选人表", counts["candidates"]],
                        ["岗位画像", counts["position_profiles"]],
                        ["候选人画像", counts["candidate_profiles"]],
                        ["人岗匹配", counts["candidate_intelligence"]],
                        ["回复", counts["candidate_replies"]],
                        ["待办", counts["followup_tasks"]],
                        ["触达", counts["outreach_events"]],
                        ["搜索实验", counts["search_experiments"]],
                        ["客户反馈", counts["client_feedback_events"]],
                        ["学习笔记", counts["learning_notes"]],
                    ],
                ),
                "",
                "## 边界",
                "",
                "本库保存实名结构化资料；聊天全文、简历全文、联系方式和 URL 默认不复制进 Markdown。",
            ]
        )
        self.writer.write_note("00_Index/首页.md", "猎聘私密主库", "index", ["liepin", "private", "home"], body)

    def write_sync_log(self, data: dict[str, list[sqlite3.Row]], candidate_items: list[dict[str, Any]]) -> None:
        body = "\n".join(
            [
                f"同步时间：{self.now}",
                "",
                table(
                    ["对象", "数量"],
                    [
                        ["候选人 notes", len(candidate_items)],
                        ["回复 notes", len(data["candidate_replies"])],
                        ["待办 notes", len(data["followup_tasks"])],
                        ["触达 notes", len(data["outreach_events"])],
                        ["客户反馈 notes", len(data["client_feedback_events"])],
                        ["搜索实验 notes", len(data["search_experiments"])],
                    ],
                ),
                "",
                "## 写入统计",
                "",
                table(
                    ["类型", "数量"],
                    [
                        ["created", self.writer.stats.created],
                        ["updated", self.writer.stats.updated],
                        ["unchanged", self.writer.stats.unchanged],
                        ["planned_create", self.writer.stats.planned_create],
                        ["planned_update", self.writer.stats.planned_update],
                    ],
                ),
            ]
        )
        self.writer.write_note("90_System/同步日志.md", "同步日志", "log", ["liepin", "sync", "private"], body)

    def write_public_summary(self, data: dict[str, list[sqlite3.Row]], candidate_items: list[dict[str, Any]]) -> WriteStats:
        writer = VaultWriter(self.public_vault, dry_run=self.dry_run)
        if not self.dry_run:
            (self.public_vault / "60_Cases").mkdir(parents=True, exist_ok=True)
        client_counts = Counter(clean(row_get(row, "client")) or "未定客户" for row in data["candidates"])
        status_counts = Counter(clean(row_get(row, "status")) or "new" for row in data["candidates"])
        body = "\n".join(
            [
                f"同步时间：{self.now}",
                "",
                "本页来自猎聘私密主库的脱敏聚合摘要；不包含候选人姓名、联系方式、聊天原文或简历正文。",
                "",
                "## 聚合概览",
                "",
                table(
                    ["数据项", "数量"],
                    [
                        ["私密人选页", len(candidate_items)],
                        ["候选人结构化记录", len(data["candidates"])],
                        ["人岗匹配记录", len(data["candidate_intelligence"])],
                        ["回复结构化记录", len(data["candidate_replies"])],
                        ["待办结构化记录", len(data["followup_tasks"])],
                        ["触达结构化记录", len(data["outreach_events"])],
                        ["搜索实验", len(data["search_experiments"])],
                        ["客户反馈", len(data["client_feedback_events"])],
                    ],
                ),
                "",
                "## 客户分布",
                "",
                table(["客户", "结构化人选数"], [[client, count] for client, count in client_counts.most_common(20)]),
                "",
                "## 状态分布",
                "",
                table(["状态", "数量"], [[status, count] for status, count in status_counts.most_common(20)]),
                "",
                "## 后续沉淀规则",
                "",
                "- 公开知识库只沉淀客户偏好、岗位画像、人才地图、复盘和打法。",
                "- 人选实名、聊天、触达和待办留在私密主库。",
            ]
        )
        writer.write_note(
            "60_Cases/猎聘智能项目复盘.md",
            "猎聘智能项目复盘",
            "case",
            ["liepin", "review", "sensitive-reviewed"],
            body,
        )
        return writer.stats

    def run(self) -> dict[str, Any]:
        self.writer.ensure_dirs()
        data = self.load_data()
        candidate_items = self.prepare_note_names(data)
        self.build_indexes(data, candidate_items)
        self.write_system_pages()
        self.write_search_pages(data)
        self.write_client_and_role_pages(data)
        self.write_interaction_pages(data)
        self.write_candidate_pages(data, candidate_items)
        self.write_reviews(data)
        self.write_home(data, candidate_items)
        self.write_sync_log(data, candidate_items)
        public_stats = self.write_public_summary(data, candidate_items)
        return {
            "ok": True,
            "dry_run": self.dry_run,
            "private_vault": str(self.private_vault),
            "public_vault": str(self.public_vault),
            "counts": {key: len(value) for key, value in data.items()},
            "candidate_notes": len(candidate_items),
            "private_stats": self.writer.stats.__dict__,
            "public_stats": public_stats.__dict__,
        }


def write_receipt(output_dir: Path, result: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘Obsidian私密主库同步_{stamp}.md"
    private_stats = result["private_stats"]
    public_stats = result["public_stats"]
    lines = [
        "# 猎聘 Obsidian 私密主库同步",
        "",
        f"生成时间：{sync_time()}",
        f"模式：{'dry-run' if result['dry_run'] else 'write'}",
        "",
        "## 路径",
        "",
        f"- 私密库：{result['private_vault']}",
        f"- 公开知识库：{result['public_vault']}",
        "",
        "## 数据量",
        "",
        table(
            ["对象", "数量"],
            [
                ["人选 notes", result["candidate_notes"]],
                ["候选人记录", result["counts"].get("candidates", 0)],
                ["人岗匹配", result["counts"].get("candidate_intelligence", 0)],
                ["回复", result["counts"].get("candidate_replies", 0)],
                ["待办", result["counts"].get("followup_tasks", 0)],
                ["触达", result["counts"].get("outreach_events", 0)],
                ["搜索实验", result["counts"].get("search_experiments", 0)],
                ["客户反馈", result["counts"].get("client_feedback_events", 0)],
            ],
        ),
        "",
        "## 写入统计",
        "",
        table(
            ["库", "created", "updated", "unchanged", "planned_create", "planned_update"],
            [
                [
                    "私密库",
                    private_stats["created"],
                    private_stats["updated"],
                    private_stats["unchanged"],
                    private_stats["planned_create"],
                    private_stats["planned_update"],
                ],
                [
                    "公开知识库",
                    public_stats["created"],
                    public_stats["updated"],
                    public_stats["unchanged"],
                    public_stats["planned_create"],
                    public_stats["planned_update"],
                ],
            ],
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Liepin SQLite data into a private Obsidian vault.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--private-vault", default=str(DEFAULT_PRIVATE_VAULT))
    parser.add_argument("--public-vault", default=str(DEFAULT_PUBLIC_VAULT))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    private_vault = Path(args.private_vault).expanduser()
    public_vault = Path(args.public_vault).expanduser()
    output_dir = Path(args.output_dir).expanduser()

    with connect(db_path) as conn:
        syncer = LiepinObsidianSync(conn, private_vault, public_vault, output_dir, dry_run=args.dry_run)
        result = syncer.run()
    receipt = write_receipt(output_dir, result)
    result["receipt"] = str(receipt)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
