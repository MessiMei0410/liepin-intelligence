from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
import urllib.parse
import urllib.request
from typing import Any


EXTRACT_RESUME_JS = r"""
(async () => {
  const clean = value => String(value || '').replace(/\u00a0/g, ' ').replace(/[ \t]+/g, ' ').trim();
  const expanders = [...document.querySelectorAll('button,a,span,div')].filter(element =>
    element.offsetParent !== null &&
    /^显示其他\d+段(?:工作|项目|教育)?经历$/.test(clean(element.innerText)) &&
    ![...element.children].some(child => clean(child.innerText) === clean(element.innerText))
  );
  for (const element of expanders.slice(0, 12)) element.click();
  if (expanders.length) await new Promise(resolve => setTimeout(resolve, 500));
  const helper = document.getElementById('liepin-reply-assistant-root');
  const previousDisplay = helper?.style.display || '';
  if (helper) helper.style.display = 'none';
  const raw = document.body?.innerText || '';
  if (helper) helper.style.display = previousDisplay;
  const lines = raw.split('\n').map(clean).filter(Boolean).filter(line =>
    !/每日任务|你好，|我的主页|个人中心|安全中心|账户资源|用户规则|通话管理|安全退出|ICP备|查看大图/.test(line)
  );
  const section = (startRe, endRe) => {
    const start = lines.findIndex(line => startRe.test(line));
    if (start < 0) return '';
    const rest = lines.slice(start + 1);
    const end = rest.findIndex(line => endRe.test(line));
    return (end >= 0 ? rest.slice(0, end) : rest).join('\n');
  };
  const isName = line => /^[\u4e00-\u9fa5]{1,4}(?:\*{1,2}|先生|女士|老师)?$/.test(line) &&
    !/简历|洞察|中文|英文|查看|收藏|转发|在线|活跃/.test(line);
  let name = clean(document.querySelector('.new-resume-personal-name em')?.innerText || '');
  if (!isName(name)) name = '';
  if (!name) name = lines.find((line, index) => isName(line) && /在职|离职|暂无跳槽|看看新机会|急寻新工作/.test(lines[index + 1] || '')) || '';
  const nameIndex = lines.findIndex(line => line === name);
  const resumeLines = lines.slice(nameIndex >= 0 ? nameIndex : 0);
  const resumeEnd = resumeLines.findIndex(line =>
    /^(?:声明：该人选信息|简历备注|简历洞察|ICP经营许可证|人才服务许可证)/.test(line)
  );
  const cleanResumeLines = (resumeEnd >= 0 ? resumeLines.slice(0, resumeEnd) : resumeLines).filter(line =>
    !/^(?:继续沟通|超级聊聊推荐职位|查看联系方式|收藏|转发|询问TA|显示其他\d+段(?:工作|项目|教育)?经历)$/.test(line)
  );
  const nearName = nameIndex >= 0 ? lines.slice(nameIndex + 1, nameIndex + 10) : lines.slice(0, 30);
  const status = nearName.find(line => /在职|离职|暂无跳槽|看看新机会|急寻新工作|暂不考虑|不看机会/.test(line)) || '';
  const meta = nearName.find(line => /(?:男|女).*?(?:工作\d+年|应届)/.test(line)) || '';
  const workText = section(/工作经历/, /项目经历|教育经历|语言能力|我的技能|自我评价|附加信息|简历备注/);
  const projectText = section(/项目经历/, /教育经历|语言能力|我的技能|自我评价|附加信息|简历备注|资格证书|培训经历|作品|附件/);
  const educationText = section(/教育经历/, /语言能力|我的技能|自我评价|附加信息|简历备注|资格证书|培训经历|作品|附件/);
  const workLines = workText.split('\n').map(clean).filter(Boolean);
  const dateIndex = workLines.findIndex(line => /\d{4}\.\d{2}\s*-\s*(?:至今|\d{4}\.\d{2})/.test(line));
  const company = dateIndex > 0 ? workLines.slice(0, dateIndex).reverse().find(line => !/工作地点|职责业绩|下属人数/.test(line)) || '' : '';
  const title = dateIndex >= 0 ? workLines.slice(dateIndex + 1, dateIndex + 8).find(line => /工程师|经理|专家|主管|负责人|架构师|设计师|研发|总监|部长|主任|产品经理|leader|manager|engineer/i.test(line) && line.length < 80) || '' : '';
  const resumeId = new URL(location.href).searchParams.get('res_id_encode') || '';
  const education = (meta.match(/博士|硕士|本科|大专|高中/) || educationText.match(/博士|硕士|本科|大专|高中/) || [''])[0];
  const experience = (meta.match(/工作\d+年/) || [''])[0].replace('工作', '');
  const city = clean((meta.match(/(?:男|女)\d+岁([^\d]{2,12}?)(?:博士|硕士|本科|大专|高中)/) || [,''])[1]);
  return JSON.stringify({
    resume_id: resumeId,
    source_url: location.href,
    name: clean(name),
    status: clean(status),
    company: clean(company),
    title: clean(title),
    city,
    education,
    experience,
    work_text: workText.slice(0, 30000),
    project_text: projectText.slice(0, 30000),
    education_text: educationText.slice(0, 12000),
    full_text: cleanResumeLines.join('\n').slice(0, 60000),
    captured_at: new Date().toISOString()
  });
})()
"""


class CDP:
    def __init__(self, ws_url: str) -> None:
        parsed = urllib.parse.urlparse(ws_url)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(15)
        self.sock.connect((str(parsed.hostname), int(parsed.port or 80)))
        self._id = 0
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {parsed.path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            response += self.sock.recv(4096)
        if b"101" not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError("无法连接猎聘 Chrome CDP")

    def close(self) -> None:
        self.sock.close()

    def evaluate(self, expression: str) -> Any:
        self._id += 1
        payload = json.dumps(
            {
                "id": self._id,
                "method": "Runtime.evaluate",
                "params": {"expression": expression, "returnByValue": True, "awaitPromise": True},
            },
            ensure_ascii=False,
        )
        self.sock.sendall(self._frame(payload))
        response = self._receive()
        if not response:
            raise RuntimeError("猎聘页面未返回简历内容")
        result = response.get("result", {}).get("result", {})
        if result.get("subtype") == "error":
            raise RuntimeError("猎聘简历页面解析失败")
        return result.get("value")

    @staticmethod
    def _frame(text: str) -> bytes:
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
        return bytes(header) + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(data))

    def _receive(self) -> dict[str, Any] | None:
        deadline = time.time() + 15
        while time.time() < deadline:
            header = self.sock.recv(2)
            if len(header) < 2:
                return None
            opcode = header[0] & 0x0F
            length = header[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self.sock.recv(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self.sock.recv(8))[0]
            mask = self.sock.recv(4) if header[1] & 0x80 else None
            data = b""
            while len(data) < length:
                data += self.sock.recv(min(length - len(data), 65536))
            if mask:
                data = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
            if opcode != 0x01:
                continue
            message = json.loads(data.decode())
            if message.get("id") == self._id:
                return message
        return None


def _http_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


def capture_open_liepin_resumes(port: int = 9223) -> list[dict[str, Any]]:
    try:
        tabs = _http_json(f"http://127.0.0.1:{int(port)}/json/list")
    except Exception as exc:
        raise RuntimeError(f"无法连接猎聘 Chrome CDP 端口 {port}") from exc
    candidates = [
        tab for tab in tabs
        if tab.get("type") == "page" and "liepin.com/resume/showresumedetail" in str(tab.get("url") or "")
    ]
    if not candidates:
        raise RuntimeError("没有找到已打开的猎聘简历详情页")
    resumes: list[dict[str, Any]] = []
    for tab in candidates:
        ws_url = str(tab.get("webSocketDebuggerUrl") or "")
        if not ws_url:
            continue
        client = CDP(ws_url)
        try:
            raw = client.evaluate(EXTRACT_RESUME_JS)
        finally:
            client.close()
        try:
            resume = json.loads(str(raw or "{}"))
        except json.JSONDecodeError:
            continue
        if resume.get("resume_id") and len(str(resume.get("full_text") or "")) >= 100:
            resumes.append(resume)
    if not resumes:
        raise RuntimeError("猎聘简历详情页尚未加载出可读内容")
    return resumes


def _normalized(value: Any) -> str:
    return "".join(str(value or "").lower().split())


def _masked_name(value: Any) -> bool:
    text = str(value or "")
    return any(token in text for token in ("*", "某", "先生", "女士", "老师"))


def resume_matches_identity(identity: dict[str, Any], resume: dict[str, Any]) -> bool:
    expected_name = _normalized(identity.get("name"))
    actual_name = _normalized(resume.get("name"))
    expected_company = _normalized(identity.get("company"))
    expected_title = _normalized(identity.get("title"))
    resume_company = _normalized(resume.get("company"))
    resume_title = _normalized(resume.get("title"))
    resume_text = _normalized(resume.get("full_text"))
    company_match = bool(expected_company and (expected_company == resume_company or expected_company in resume_text))
    title_match = bool(expected_title and (expected_title == resume_title or expected_title in resume_text))
    if _masked_name(identity.get("name")):
        surname_match = bool(expected_name and actual_name and expected_name[0] == actual_name[0])
        return surname_match and company_match and title_match
    name_match = bool(expected_name and expected_name == actual_name)
    return name_match and (company_match or title_match)
