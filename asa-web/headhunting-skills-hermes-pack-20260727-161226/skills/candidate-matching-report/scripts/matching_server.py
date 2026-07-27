#!/usr/bin/env python3
"""
候选人-岗位匹配分析 HTTP 服务（纯 Codex 版，无 Hermes 依赖）
v2.0.0

架构变化（相比 v1.x）：
  - 队列目录从 ~/.hermes/matching_queue/ 迁移到 ~/.codex/matching_queue/
  - 不再依赖 Hermes cron 任务；内置后台 Worker 线程直接调用 Anthropic API
  - 支持最多 2 个并发 Worker（队列 >= 3 时自动扩展）

启动方式（推荐 LaunchAgent 守护）：
  python3 matching_server.py

API:
  POST /match        - 提交匹配请求（JD须 >= 200 字）
  GET  /status       - 全局队列状态
  GET  /status?id=xx - 单个请求进度
"""

import json
import os
import sys
import time
import signal
import hashlib
import glob
import threading
import queue
import subprocess
import tempfile
import requests as http_client
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ── 配置 ──────────────────────────────────────────────────────
QUEUE_DIR     = os.path.expanduser("~/.codex/matching_queue")
OUTPUT_DIR    = os.path.expanduser("~/Desktop/匹配分析")
PORT          = 18901
WAIT_TIMEOUT  = 120
JD_MIN_LENGTH = 200
MAX_WORKERS   = 2
SKILL_DIR     = Path(__file__).parent.parent  # ~/.codex/skills/candidate-matching-report
TEMPLATE_PY   = SKILL_DIR / "scripts" / "report_template.py"

# Anthropic API（使用本地代理，读取环境变量）
API_BASE  = os.environ.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:15721")
API_TOKEN = os.environ.get("ANTHROPIC_AUTH_TOKEN", "PROXY_MANAGED")
MODEL     = "claude-fable-5"

os.makedirs(QUEUE_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── 全局状态 ──────────────────────────────────────────────────
_status_map: dict[str, dict] = {}   # req_id → 状态字典
_work_queue: queue.Queue = queue.Queue()
_workers_started = 0
_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────
# 简历文本提取
# ─────────────────────────────────────────────────────────────
def extract_resume_text(resume_path: str) -> str:
    """从 .docx 文件提取纯文本，失败时尝试 markitdown。"""
    path = Path(resume_path)
    if not path.exists():
        return f"[错误] 简历文件不存在：{resume_path}"

    # 方案一：python-docx
    try:
        from docx import Document
        doc = Document(str(path))
        text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        if text.strip():
            return text
    except Exception:
        pass

    # 方案二：markitdown
    try:
        result = subprocess.run(
            ["markitdown", str(path)],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except Exception:
        pass

    return f"[错误] 无法提取简历文本，请确认文件格式：{resume_path}"


# ─────────────────────────────────────────────────────────────
# Anthropic API 调用
# ─────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """你是一位专业猎头分析师，擅长生成结构化的候选人-岗位匹配分析报告。

你的任务：根据候选人简历文本和岗位 JD，严格按照下面的 JSON Schema 输出匹配分析结果。
只输出合法的 JSON 对象，不要有任何前缀说明或代码块标记。

JSON 结构：
{
  "candidate": "候选人姓名",
  "company": "目标公司名",
  "position": "目标岗位",
  "plugin_version": "v2.0.0",
  "jd_incomplete_warning": "如果JD不足200字在此说明，否则留空字符串",
  "hard_gates": [
    ["要求项", "候选人情况描述", "✅/⚠️/❌"]
  ],
  "target_company_background": "目标公司主营业务、规模阶段、行业定位（2-3句）",
  "responsibility_matches": [
    {"duty": "JD职责", "stars": 4, "evidence": "简历中的具体佐证，如候选人来自竞品公司则加注🏆竞品背景"}
  ],
  "bonus_items": [
    ["加分项", "候选人情况", "✅/⚠️"]
  ],
  "risks": [
    {
      "level": "高/中/低",
      "title": "风险标题",
      "description": "具体描述",
      "verify": "电话或面试中的验证建议"
    }
  ],
  "scores": {
    "维度名": 分数
  },
  "total_score": 综合分,
  "interview_suggestions": [
    ["考察维度", "建议问题话术"]
  ],
  "phone_guide": {
    "stages": {
      "阶段名（时长）": "话术内容"
    },
    "checklist": [
      ["考察维度", "通过标准", "红灯信号"]
    ],
    "taboos": ["禁忌1", "禁忌2"]
  },
  "verdict": "🏆 强推面试 / 可推面试 / 观望 / 不推",
  "conclusion_summary": "总结性段落"
}

硬性要求：
1. hard_gates 必须包含：学历专业、工作年限、行业背景、目前公司主营业务（含具体业务方向和✅⚠️❌判定）、薪资区间对照、跳槽频率（计算平均在职月数，<18个月标⚠️，<12个月标❌）
2. target_company_background 必须根据 JD 推断目标公司背景
3. scores 维度根据岗位类型动态选择4-5个最相关维度（主营业务匹配为必选），不相关维度不写
4. 主营业务匹配评分：80-100完全一致，60-79高度相关，40-59部分交叉，0-39跨行业
5. risks 每条必须有 level（高/中/低）、title、description、verify 四个字段
6. 如果 JD 不完整（<200字），jd_incomplete_warning 填写警告文本，hard_gates 各行判定标 ⚠️
"""

def call_anthropic(resume_text: str, jd_text: str,
                   candidate: str, company: str, position: str) -> dict:
    """调用 Anthropic API 生成分析 JSON。"""
    user_content = f"""候选人：{candidate}
目标公司：{company}
目标岗位：{position}
JD字数：{len(jd_text.strip())}字

===== 简历全文 =====
{resume_text}

===== 岗位 JD =====
{jd_text}

请按照系统提示中的 JSON 结构输出分析结果。"""

    resp = http_client.post(
        f"{API_BASE}/v1/messages",
        headers={
            "x-api-key": API_TOKEN,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 8192,
            "system": _SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_content}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    raw = resp.json()["content"][0]["text"].strip()

    # 去掉可能的 ```json ... ``` 包裹
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    return json.loads(raw)


# ─────────────────────────────────────────────────────────────
# docx 生成
# ─────────────────────────────────────────────────────────────
def generate_docx(analysis: dict, output_path: str) -> bool:
    """把分析 JSON 写入临时文件，调用 report_template.py 生成 .docx。"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", encoding="utf-8", delete=False
    ) as f:
        json.dump(analysis, f, ensure_ascii=False)
        tmp_path = f.name

    try:
        result = subprocess.run(
            [sys.executable, str(TEMPLATE_PY),
             "--data", f"@{tmp_path}",
             "--output", output_path],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            print(f"[worker] report_template 错误: {result.stderr}", flush=True)
            return False
        return Path(output_path).exists()
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# Worker 线程
# ─────────────────────────────────────────────────────────────
def _worker_loop():
    while True:
        req = _work_queue.get()
        if req is None:
            break
        req_id = req["id"]
        try:
            _set_status(req_id, "processing", "正在提取简历文本...")
            resume_text = extract_resume_text(req["resume_path"])

            _set_status(req_id, "processing", "正在调用 AI 生成分析...")
            analysis = call_anthropic(
                resume_text,
                req["jd_text"],
                req["candidate"],
                req["company"],
                req["position"],
            )

            output_path = req.get("output_path") or os.path.join(
                OUTPUT_DIR,
                f"人选匹配_{req['candidate']}_{req['company']}_{req['position']}_{req_id}.docx"
            )
            _set_status(req_id, "processing", "正在生成报告文件...")
            ok = generate_docx(analysis, output_path)

            if ok:
                _set_status(req_id, "done", "报告已生成", output_path=output_path)
                print(f"[{_ts()}] ✓ 完成: {req['candidate']} → {output_path}", flush=True)
            else:
                _set_status(req_id, "error", "docx 生成失败")

            # 删除队列文件
            req_file = os.path.join(QUEUE_DIR, f"req_{req_id}.json")
            try:
                os.unlink(req_file)
            except Exception:
                pass

        except Exception as e:
            _set_status(req_id, "error", str(e))
            print(f"[{_ts()}] ✗ 失败 ({req_id}): {e}", flush=True)
        finally:
            _work_queue.task_done()


def _set_status(req_id: str, state: str, message: str, output_path: str = ""):
    with _lock:
        _status_map[req_id] = {
            "id": req_id,
            "state": state,
            "message": message,
            "output": output_path,
            "updated_at": _ts(),
        }


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def _ensure_workers(needed: int = 1):
    """按需启动 Worker 线程，最多 MAX_WORKERS 个。"""
    global _workers_started
    with _lock:
        to_start = min(needed, MAX_WORKERS) - _workers_started
        for _ in range(to_start):
            t = threading.Thread(target=_worker_loop, daemon=True)
            t.start()
            _workers_started += 1


# ─────────────────────────────────────────────────────────────
# 启动时恢复未处理的队列文件
# ─────────────────────────────────────────────────────────────
def _restore_queue():
    pending = sorted(
        [f for f in Path(QUEUE_DIR).glob("req_*.json")],
        key=lambda f: f.stat().st_mtime
    )
    if not pending:
        return
    print(f"[{_ts()}] 恢复 {len(pending)} 个未完成请求", flush=True)
    workers_needed = min(len(pending), MAX_WORKERS)
    _ensure_workers(workers_needed)
    for p in pending:
        try:
            req = json.loads(p.read_text(encoding="utf-8"))
            _set_status(req["id"], "queued", "等待处理（服务重启后恢复）")
            _work_queue.put(req)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# HTTP Handler
# ─────────────────────────────────────────────────────────────
class MatchHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send_json({"ok": True})

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/status":
            req_id = params.get("id", [None])[0]
            if req_id:
                with _lock:
                    info = _status_map.get(req_id)
                if info:
                    self._send_json(info)
                else:
                    # 检查磁盘队列
                    req_file = os.path.join(QUEUE_DIR, f"req_{req_id}.json")
                    if os.path.exists(req_file):
                        self._send_json({"id": req_id, "state": "queued",
                                         "message": "等待 Worker 处理"})
                    else:
                        self._send_json({"id": req_id, "state": "not_found"}, 404)
            else:
                pending_files = list(Path(QUEUE_DIR).glob("req_*.json"))
                with _lock:
                    in_progress = [v for v in _status_map.values()
                                   if v["state"] in ("queued", "processing")]
                self._send_json({
                    "status": "running",
                    "queue_on_disk": len(pending_files),
                    "in_progress": len(in_progress),
                    "workers": _workers_started,
                })
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if urlparse(self.path).path != "/match":
            self._send_json({"error": "not found"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length))

            resume_path = data.get("resume_path", "")
            jd_text     = data.get("jd_text", "")
            candidate   = data.get("candidate", "候选人")
            company     = data.get("company", "企业")
            position    = data.get("position", "岗位")

            if not resume_path or not jd_text:
                self._send_json({"error": "resume_path 和 jd_text 必填"}, 400)
                return

            if len(jd_text.strip()) < JD_MIN_LENGTH:
                self._send_json({
                    "error": (
                        f"JD 内容过短（{len(jd_text.strip())} 字），"
                        f"请补充完整的岗位职责、任职要求和加分项（至少 {JD_MIN_LENGTH} 字）"
                    )
                }, 400)
                return

            req_id = hashlib.md5(
                f"{resume_path}{datetime.now().isoformat()}".encode()
            ).hexdigest()[:8]

            output_path = data.get("output_path") or os.path.join(
                OUTPUT_DIR,
                f"人选匹配_{candidate}_{company}_{position}_{req_id}.docx"
            )

            req = {
                "id": req_id,
                "candidate": candidate,
                "company": company,
                "position": position,
                "resume_path": resume_path,
                "jd_text": jd_text,
                "output_path": output_path,
                "submitted_at": datetime.now().isoformat(),
            }

            # 持久化到磁盘（服务重启可恢复）
            req_file = os.path.join(QUEUE_DIR, f"req_{req_id}.json")
            with open(req_file, "w", encoding="utf-8") as f:
                json.dump(req, f, ensure_ascii=False, indent=2)

            _set_status(req_id, "queued", "已入队，等待处理")

            # 按需启动 Worker
            queue_size = _work_queue.qsize() + 1
            _ensure_workers(2 if queue_size >= 3 else 1)
            _work_queue.put(req)

            print(f"[{_ts()}] 入队: {candidate} × {company} {position} ({req_id})", flush=True)

            # 短暂等待（最多 WAIT_TIMEOUT 秒），支持同步前端
            waited = 0
            while waited < WAIT_TIMEOUT:
                with _lock:
                    s = _status_map.get(req_id, {})
                if s.get("state") == "done":
                    self._send_json({
                        "success": True,
                        "id": req_id,
                        "output": s["output"],
                        "message": f"报告已生成：{Path(s['output']).name}",
                    })
                    return
                if s.get("state") == "error":
                    self._send_json({
                        "success": False,
                        "id": req_id,
                        "error": s.get("message", "未知错误"),
                    }, 500)
                    return
                time.sleep(2)
                waited += 2

            # 超时，返回 processing 状态供前端轮询
            self._send_json({
                "success": True,
                "id": req_id,
                "processing": True,
                "poll_url": f"http://127.0.0.1:{PORT}/status?id={req_id}",
                "message": f"请求已提交（{req_id}），正在生成中，请通过 poll_url 轮询",
            })

        except json.JSONDecodeError:
            self._send_json({"error": "JSON 格式错误"}, 400)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def log_message(self, format, *args):
        pass


# ─────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────
def main():
    _restore_queue()

    server = HTTPServer(("127.0.0.1", PORT), MatchHandler)

    def shutdown(sig, frame):
        print(f"\n[{_ts()}] 服务关闭")
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"[{_ts()}] 匹配服务（纯 Codex v2.0.0）→ http://127.0.0.1:{PORT}")
    print(f"  POST /match          提交请求（JD ≥ {JD_MIN_LENGTH} 字）")
    print(f"  GET  /status         全局队列状态")
    print(f"  GET  /status?id=xx   单个请求进度")
    print(f"  队列目录: {QUEUE_DIR}")
    print(f"  AI 接入: {API_BASE}  model={MODEL}")
    server.serve_forever()


if __name__ == "__main__":
    main()
