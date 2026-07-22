#!/usr/bin/env python3
"""Read Liepin IM conversations and optionally ingest candidate replies.

This script is deliberately read-only toward Liepin. It never clicks buttons,
types text, or sends messages. Default mode is dry-run.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import sqlite3
import struct
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from reply_intelligence_rules import classify_reply, normalize_message


DEFAULT_PORT = 9223
DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


EXTRACT_JS = r"""
(() => {
  const clean = s => (s || '').trim().replace(/\s+/g, ' ');
  const stripStatus = s => clean(s).replace(/^\[(?:已读|未读|手机)\]\s*/, '');
  const items = Array.from(document.querySelectorAll('.im-ui-contact-list-item'));
  const rows = items.map((el, index) => {
    const name = clean(el.querySelector('.im-ui-contact-title-main')?.innerText || '');
    const title = clean(el.querySelector('.im-ui-contact-title-sub')?.innerText || '');
    const timeText = clean(el.querySelector('.contact-time')?.innerText || '');
    const message = stripStatus(el.querySelector('.im-ui-last-message')?.innerText || el.querySelector('.im-ui-contact-item-message')?.innerText || '');
    const unreadText = clean(el.querySelector('.ant-im-badge-count-sm')?.innerText || '');
    const rawText = clean(el.innerText || el.textContent || '');
    const rect = el.getBoundingClientRect();
    return {
      index,
      name,
      title,
      timeText,
      message,
      unreadCount: unreadText ? parseInt(unreadText, 10) || 0 : 0,
      rawText,
      visible: !!(rect.width && rect.height),
    };
  }).filter(x => x.name || x.message);
  return JSON.stringify({
    url: location.href,
    title: document.title,
    extractedAt: new Date().toISOString(),
    count: rows.length,
    rows,
  });
})()
"""


SELF_MESSAGE_PATTERNS = [
    "您向对方推荐了",
    "您好，我这边是做半导体这块的",
    "您可以修改打招呼语",
    "试试超级聊聊",
]


def now_local() -> datetime:
    return datetime.now()


def normalize_preview_message(message: str) -> str:
    return normalize_message(message)


class CDP:
    def __init__(self, ws_url: str):
        u = urllib.parse.urlparse(ws_url)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(15)
        self.sock.connect((u.hostname, u.port))
        self._id = 0
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {u.path} HTTP/1.1\r\n"
            f"Host: {u.hostname}:{u.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.sock.recv(4096)
        if b"101" not in resp.split(b"\r\n")[0]:
            first_line = resp.split(b"\r\n")[0]
            raise RuntimeError(f"CDP handshake failed: {first_line!r}")

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass

    def send(self, method: str, params: dict | None = None, timeout: int = 15) -> dict | None:
        self._id += 1
        msg = json.dumps({"id": self._id, "method": method, "params": params or {}}, ensure_ascii=False)
        self.sock.sendall(self._frame(msg))
        return self._recv(timeout)

    def eval(self, expression: str, timeout: int = 15):
        res = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout=timeout,
        )
        if not res:
            return None
        inner = res.get("result", {}).get("result", {})
        return inner.get("value")

    def _frame(self, text: str) -> bytes:
        data = text.encode()
        header = bytearray([0x81])
        if len(data) < 126:
            header.append(0x80 | len(data))
        elif len(data) < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", len(data)))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", len(data)))
        mask = os.urandom(4)
        masked = bytearray(b ^ mask[i % 4] for i, b in enumerate(data))
        return bytes(header) + mask + masked

    def _recv(self, timeout: int = 15) -> dict | None:
        self.sock.settimeout(timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                hdr = self.sock.recv(2)
                if len(hdr) < 2:
                    return None
                opcode = hdr[0] & 0x0F
                length = hdr[1] & 0x7F
                if length == 126:
                    length = struct.unpack(">H", self.sock.recv(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self.sock.recv(8))[0]
                mask_key = self.sock.recv(4) if hdr[1] & 0x80 else None
                data = b""
                while len(data) < length:
                    chunk = self.sock.recv(min(length - len(data), 65536))
                    if not chunk:
                        break
                    data += chunk
                if mask_key:
                    data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
                if opcode != 0x01:
                    continue
                msg = json.loads(data.decode())
                if msg.get("id") == self._id:
                    return msg
            except socket.timeout:
                return None
        return None


def http_json(url: str) -> dict | list:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def find_im_tab(port: int) -> str:
    tabs = http_json(f"http://127.0.0.1:{port}/json/list")
    for tab in tabs:
        if tab.get("type") == "page" and "h.liepin.com/im/showmsgnewpage" in tab.get("url", ""):
            return tab["webSocketDebuggerUrl"]
    raise RuntimeError("没有找到猎聘职聊页面，请先在 Chrome 打开 https://h.liepin.com/im/showmsgnewpage")


def parse_message_time(time_text: str, extracted_at: datetime) -> str:
    text = (time_text or "").strip()
    if not text:
        return extracted_at.isoformat(timespec="seconds")
    if ":" in text:
        hour, minute = text.split(":", 1)
        try:
            value = extracted_at.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
            if value > extracted_at + timedelta(minutes=5):
                value -= timedelta(days=1)
            return value.isoformat(timespec="seconds")
        except ValueError:
            return extracted_at.isoformat(timespec="seconds")
    if text == "昨天":
        return (extracted_at - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    if "月" in text and "日" in text:
        try:
            month = int(text.split("月", 1)[0])
            day = int(text.split("月", 1)[1].split("日", 1)[0])
            value = extracted_at.replace(month=month, day=day, hour=12, minute=0, second=0, microsecond=0)
            if value > extracted_at + timedelta(days=1):
                value = value.replace(year=value.year - 1)
            return value.isoformat(timespec="seconds")
        except ValueError:
            return extracted_at.isoformat(timespec="seconds")
    return extracted_at.isoformat(timespec="seconds")


def parse_extracted_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def is_self_or_system_message(message: str) -> bool:
    text = normalize_preview_message(message)
    if not text:
        return True
    return any(pattern in text for pattern in SELF_MESSAGE_PATTERNS)


def normalize_name_for_like(name: str) -> str:
    return (name or "").replace("先生", "").replace("女士", "").replace("老师", "").strip()


def is_generic_display_name(name: str) -> bool:
    clean_name = normalize_name_for_like(name)
    return len(clean_name) <= 1 or name.endswith(("先生", "女士", "老师"))


def title_match_score(title: str, candidate: sqlite3.Row | dict) -> int:
    title_text = (title or "").lower()
    if not title_text:
        return 0
    hay = " ".join(str(candidate[k] or "") for k in ("title", "position", "company", "skills")).lower()
    score = 0
    for token in title_text.replace("/", " ").replace("&", " ").replace("-", " ").split():
        if len(token) >= 2 and token in hay:
            score += 1
    for token in ("fpga", "cim", "mes", "amhs", "pvd", "cvd", "acdc"):
        if token in title_text and token in hay:
            score += 2
    return score


def connect_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_reply_extra_schema(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(candidate_replies)")}
    extras = {
        "reply_tags_json": "TEXT DEFAULT '[]'",
        "classification_reason": "TEXT",
        "classifier_version": "TEXT",
    }
    for column, definition in extras.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE candidate_replies ADD COLUMN {column} {definition}")
    conn.commit()


def find_candidate(conn: sqlite3.Connection, name: str, title: str = "") -> dict | None:
    clean_name = normalize_name_for_like(name)
    if not clean_name:
        return None
    if is_generic_display_name(name):
        return None
    rows = conn.execute(
        """
        SELECT *
        FROM candidates
        WHERE name = ?
           OR name LIKE ?
        ORDER BY
          CASE WHEN status IN ('recommended','contacted','interviewing','offered') THEN 0 ELSE 1 END,
          updated_at DESC,
          created_at DESC
        LIMIT 8
        """,
        (name, clean_name + "%"),
    ).fetchall()
    if not rows:
        return None
    if len(rows) == 1:
        return dict(rows[0])
    scored: list[tuple[int, dict]] = []
    for row in rows:
        scored.append((title_match_score(title, row), dict(row)))
    scored.sort(key=lambda item: item[0], reverse=True)
    if scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1]
    return None


def conversation_id(row: dict) -> str:
    raw = "|".join([row.get("name", ""), row.get("title", ""), row.get("message", "")])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def extract_rows(port: int) -> dict:
    ws = find_im_tab(port)
    cdp = CDP(ws)
    try:
        raw = cdp.eval(EXTRACT_JS, timeout=20)
    finally:
        cdp.close()
    if not raw:
        raise RuntimeError("职聊页面读取失败")
    data = json.loads(raw)
    extracted_at = parse_extracted_at(data["extractedAt"])
    for row in data["rows"]:
        row["message_time"] = parse_message_time(row.get("timeText", ""), extracted_at)
        row["direction"] = "self_or_system" if is_self_or_system_message(row.get("message", "")) else "candidate"
        row["conversation_id"] = conversation_id(row)
        row.update(classify_reply(row.get("message", "")))
    return data


def ingest_rows(conn: sqlite3.Connection, rows: list[dict]) -> dict:
    inserted_replies = 0
    inserted_tasks = 0
    skipped_self = 0
    unmatched = 0
    for row in rows:
        if row.get("direction") != "candidate":
            skipped_self += 1
            continue
        candidate = find_candidate(conn, row.get("name", ""), row.get("title", ""))
        if not candidate:
            unmatched += 1
        candidate_id = candidate.get("id") if candidate else None
        client = candidate.get("client") if candidate else None
        position = candidate.get("position") if candidate else None
        company = candidate.get("company") if candidate else None
        before = conn.total_changes
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO candidate_replies
              (candidate_id, candidate_name, candidate_company, client, position, channel,
               conversation_id, message_time, direction, raw_text, intent, sentiment,
               blockers_json, suggested_next_action, processed_at,
               reply_tags_json, classification_reason, classifier_version)
            VALUES (?, ?, ?, ?, ?, 'liepin', ?, ?, 'candidate', ?, ?, ?, ?, ?, datetime('now','localtime'), ?, ?, ?)
            """,
            (
                candidate_id,
                row.get("name"),
                company,
                client,
                position,
                row.get("conversation_id"),
                row.get("message_time"),
                row.get("message"),
                row.get("intent"),
                row.get("sentiment"),
                json.dumps(row.get("blockers", []), ensure_ascii=False),
                row.get("suggested_next_action"),
                json.dumps(row.get("reply_tags", []), ensure_ascii=False),
                row.get("classification_reason", ""),
                row.get("classifier_version", ""),
            ),
        )
        reply_id = cur.lastrowid
        if conn.total_changes > before:
            inserted_replies += 1
            if row.get("task_type") != "none":
                due = datetime.now() + timedelta(hours=4 if int(row.get("priority", 2)) == 1 else 24)
                conn.execute(
                    """
                    INSERT INTO followup_tasks
                      (candidate_id, candidate_name, candidate_company, client, position,
                       task_type, priority, due_at, reason, source_table, source_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate_replies', ?)
                    """,
                    (
                        candidate_id,
                        row.get("name"),
                        company,
                        client,
                        position,
                        row.get("task_type"),
                        int(row.get("priority", 2)),
                        due.isoformat(timespec="seconds"),
                        row.get("suggested_next_action"),
                        reply_id,
                    ),
                )
                inserted_tasks += 1
    conn.commit()
    return {
        "inserted_replies": inserted_replies,
        "inserted_tasks": inserted_tasks,
        "skipped_self_or_system": skipped_self,
        "unmatched_candidates": unmatched,
    }


def write_markdown(data: dict, output_path: Path, ingest_summary: dict | None = None) -> None:
    rows = data["rows"]
    lines = [
        "# 猎聘职聊回复读取报告",
        "",
        f"读取时间：{data.get('extractedAt')}",
        f"页面：`{data.get('url')}`",
        f"会话数：{len(rows)}",
        "",
    ]
    if ingest_summary:
        lines.extend([
            "## 入库结果",
            "",
            f"- 新增候选人回复：{ingest_summary['inserted_replies']}",
            f"- 新增跟进任务：{ingest_summary['inserted_tasks']}",
            f"- 跳过我方/系统消息：{ingest_summary['skipped_self_or_system']}",
            f"- 未匹配人才库候选人：{ingest_summary['unmatched_candidates']}",
            "",
        ])
    lines.extend([
        "## 可行动回复",
        "",
        "| 序号 | 候选人 | 头衔 | 时间 | 意图 | 优先级 | 消息 | 建议动作 |",
        "|---:|---|---|---|---|---:|---|---|",
    ])
    action_rows = [row for row in rows if row.get("direction") == "candidate"]
    for row in action_rows:
        message = (row.get("message") or "").replace("|", "｜")
        action = (row.get("suggested_next_action") or "").replace("|", "｜")
        lines.append(
            f"| {row.get('index')} | {row.get('name','')} | {row.get('title','')} | {row.get('timeText','')} | {row.get('intent','')} | {row.get('priority','')} | {message} | {action} |"
        )
    if not action_rows:
        lines.append("| - | - | - | - | - | - | 暂无候选人新回复 | - |")

    lines.extend([
        "",
        "## 跳过的我方/系统消息",
        "",
    ])
    skipped = [row for row in rows if row.get("direction") != "candidate"]
    for row in skipped[:20]:
        lines.append(f"- {row.get('name','')}：{row.get('message','')}")

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read Liepin IM replies and optionally ingest them into the intelligence DB.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--mode", choices=["dry-run", "ingest"], default="dry-run")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    if args.mode == "ingest" and args.confirm != "INGEST":
        raise SystemExit("拒绝入库：必须同时提供 --mode ingest --confirm INGEST")

    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    data = extract_rows(args.port)

    ingest_summary = None
    if args.mode == "ingest":
        conn = connect_db(Path(args.db).expanduser())
        try:
            ensure_reply_extra_schema(conn)
            ingest_summary = ingest_rows(conn, data["rows"])
        finally:
            conn.close()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"liepin_im_replies_{args.mode}_{stamp}.json"
    md_path = output_dir / f"猎聘职聊回复读取报告_{args.mode}_{stamp}.md"
    json_path.write_text(json.dumps({"data": data, "ingest_summary": ingest_summary}, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(data, md_path, ingest_summary)

    summary = {
        "ok": True,
        "mode": args.mode,
        "visible_conversations": data.get("count", 0),
        "candidate_replies": sum(1 for row in data["rows"] if row.get("direction") == "candidate"),
        "self_or_system": sum(1 for row in data["rows"] if row.get("direction") != "candidate"),
        "json": str(json_path),
        "markdown": str(md_path),
        "ingest_summary": ingest_summary,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
