#!/usr/bin/env python3
"""ASA ← DSH 桥接服务（路 2 · Phase 4 v0）。

把 DSH headless（`asa` profile）包装成可调用的 HTTP Agent 接口，供 ASA Core 或前端调用：
  POST /turn   {message, context?} -> {ok, answer}
  GET  /health -> {ok, profile}

这是 Phase 4 的 v0（per-turn 子进程，非流式）。常驻/流式 sidecar 见后续阶段。

安全边界：本服务只做编排转发，不直接写库；写动作走 DSH 工具的 preflight/commit/审批，
领域情报走 asa_copilot_ask 委托现有 Copilot。正式库零接触（DSH 只调 Core HTTP API）。

环境变量：
  DSH_BIN                dsh 可执行文件（默认取当前 harness checkout）
  DSH_HOME               DSH home（默认 ~/.dsh，含 asa profile + credentials）
  ASA_DSH_BRIDGE_PORT    监听端口（默认 8890）
  ASA_DSH_TURN_TIMEOUT   单轮超时秒数（默认 180）
  ASA_DSH_CWD            dsh 工作目录（默认 /tmp/asa-dsh-spike）
"""
from __future__ import annotations

import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DSH_BIN = os.environ.get("DSH_BIN", "/Users/messi/.npm/_npx/1e7f6d9597241db0/node_modules/.bin/dsh")
DSH_HOME = os.environ.get("DSH_HOME", os.path.expanduser("~/.dsh"))
CWD = os.environ.get("ASA_DSH_CWD", "/tmp/asa-dsh-spike")
PORT = int(os.environ.get("ASA_DSH_BRIDGE_PORT", "8890"))
TIMEOUT = int(os.environ.get("ASA_DSH_TURN_TIMEOUT", "180"))


def run_turn(message: str) -> dict:
    env = dict(os.environ)
    env["DSH_HOME"] = DSH_HOME
    proc = subprocess.run(
        [DSH_BIN, "--profile", "asa", message],
        capture_output=True, text=True, timeout=TIMEOUT, env=env, cwd=CWD,
    )
    answer = proc.stdout.strip()
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or f"dsh exit {proc.returncode}")[-2000:], "answer": answer}
    return {"ok": True, "answer": answer}


class Handler(BaseHTTPRequestHandler):
    # 仅监听 127.0.0.1 的本地桥接；CORS 放行同机前端（8765）。生产硬化时收紧到具体 origin。
    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Idempotency-Key")

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"ok": True, "profile": "asa", "dsh_home": DSH_HOME})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/turn":
            self._json(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:  # noqa: BLE001
            self._json(400, {"ok": False, "error": f"bad json: {exc}"})
            return
        message = str(req.get("message", "")).strip()
        if not message:
            self._json(400, {"ok": False, "error": "message is required"})
            return
        try:
            result = run_turn(message)
        except subprocess.TimeoutExpired:
            result = {"ok": False, "error": "dsh turn timed out"}
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)}
        self._json(200, result)

    def log_message(self, *args) -> None:  # noqa: ARG002
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"ASA<->DSH bridge on http://127.0.0.1:{PORT} (profile=asa)", flush=True)
    server.serve_forever()
