"""R12-a 浮窗 parity 补齐的回归守护。

覆盖两项浮窗侧改动（均为 additive，legacy 语义不变）：
1. business_focus 焦点条——浮窗渲染每条 copilot 响应与
   /api/agent/copilot/session 恢复响应里的 business_focus 字段，
   含 needs_clarification 冲突态（语义对齐 React Copilot 焦点卡）。
2. 发送切 v1——sendMessage 经 postCopilotMessage 打
   /api/v1/copilot/messages，Idempotency-Key=floating-{sessionId}-{messageHash}
   （FNV-1a 32 位稳定散列），request_id 同理派生；v1 失败回退
   legacy /api/agent/copilot 一次并 console 留痕。

幂等 key 派生函数的单测通过 node 真实执行浮窗 HTML 里的 JS 完成
（tests 下既有 asa_floating_attachment_ui.spec.js 同款思路：直接验证
交付给浏览器的实际源码，而不是 Python 侧的重写版）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER_SOURCE = ROOT / "scripts" / "liepin_workbench_server.py"


def _server_text() -> str:
    return SERVER_SOURCE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. business_focus 焦点条：源码契约
# ---------------------------------------------------------------------------


def test_floating_focus_bar_rendering_contract() -> None:
    source = _server_text()
    for marker in [
        'id="focusBar"',
        ".focus-bar.empty",
        ".focus-bar.conflict",
        "renderFocusBar",
        "focusBarLabel",
        "FOCUS_ACTION_LABELS",
        "state.businessFocus",
        "result.business_focus",
        "needs_clarification === true",
        "需要确认",
        "当前焦点",
        "focus?.candidate?.name",
        "focus?.client, focus?.job?.title",
        "focus.directions.join(' / ')",
        "businessFocus:null",
    ]:
        assert marker in source, marker
    # React 语义对齐：候选人姓名优先，其次「客户 / 岗位标题」，再退 client；
    # 无 focus（旧 Core 无该字段）时不渲染。
    assert "focus?.candidate?.name || [focus?.client, focus?.job?.title].filter(Boolean).join(' / ') || focus?.client || ''" in source
    # 与 React actionLabel 完全一致的 8 个动作标签
    for action_label in [
        "job_archive:'归档岗位'",
        "job_split:'拆分岗位'",
        "job_publish:'发布岗位'",
        "candidate_sourcing:'寻访人选'",
        "candidate_outreach:'触达人选'",
        "candidate_review:'复核人选'",
        "recommendation:'客户推荐'",
        "salary:'谈薪处理'",
    ]:
        assert action_label in source, action_label


def test_floating_focus_bar_data_sources_contract() -> None:
    source = _server_text()
    # 三条数据源路径都把 business_focus 落到 state.businessFocus：
    # sendMessage 响应、restoreCurrentSession、loadSession。
    assert source.count("state.businessFocus = result.business_focus;") == 3
    # 焦点条渲染挂进 renderMessages，且焦点条在 context-panels 之外
    # （updateContextPanelsVisibility 语义不变）。
    assert "renderAttachments();\n  renderFocusBar();" in source
    assert source.index('id="focusBar"') < source.index('class="context-panels"')
    # 新对话清空焦点
    assert "state.businessFocus=null;" in source


# ---------------------------------------------------------------------------
# 2. 发送切 v1 + 幂等 + 回退：源码契约
# ---------------------------------------------------------------------------


def test_floating_copilot_transport_contract() -> None:
    source = _server_text()
    for marker in [
        "// --- asa-floating-copilot-transport ---",
        "// --- end asa-floating-copilot-transport ---",
        "/api/v1/copilot/messages",
        "Idempotency-Key",
        "request_id",
        "floatingMessageHash",
        "floatingCopilotIdempotencyKey",
        "floatingCopilotRequestId",
        "postCopilotMessage",
        "0x811c9dc5",
        "0x01000193",
        "Math.imul",
        "console.warn",
        "回退 /api/agent/copilot 重试一次",
        "timeoutMs:45000",
    ]:
        assert marker in source, marker
    # 幂等键规格：floating-{sessionId}-{messageHash}；request_id 同理派生
    assert "return `floating-${sessionId}-${floatingMessageHash(text)}`;" in source
    assert "return `floating_req_${sessionId}_${floatingMessageHash(text)}`;" in source


def test_floating_send_message_uses_v1_with_legacy_fallback() -> None:
    source = _server_text()
    send_message = source.split("async function sendMessage()", 1)[1].split("\nloadState();", 1)[0]
    assert "postCopilotMessage(" in send_message
    assert "floatingCopilotIdempotencyKey(state.sessionId, text)" in send_message
    assert "floatingCopilotRequestId(state.sessionId, text)" in send_message
    assert "state.businessFocus = result.business_focus;" in send_message
    # sendMessage 不再直连 legacy；legacy 直连只保留在 postCopilotMessage 回退分支
    # 与本机图片/附件重答链路（answerAfterNativeImage/Attachment，不在本任务范围）。
    assert "api('/api/agent/copilot'" not in send_message
    # workflow_id 尾注纪律不变：仅 result.workflow_id 为真时追加
    assert "result.workflow_id ?" in send_message
    transport = source.split("// --- asa-floating-copilot-transport ---", 1)[1].split(
        "// --- end asa-floating-copilot-transport ---", 1
    )[0]
    assert transport.count("api('/api/v1/copilot/messages'") == 1
    assert transport.count("api('/api/agent/copilot'") == 1


def test_legacy_copilot_endpoint_forward_semantics_unchanged() -> None:
    source = _server_text()
    # legacy /api/agent/copilot 保持既有转发语义：core_service 优先，agent_service 兜底
    assert 'parsed.path == "/api/agent/copilot"' in source
    assert "core_service.copilot if core_service is not None else self.state.agent_service.copilot" in source


# ---------------------------------------------------------------------------
# 3. 幂等 key 派生函数单测 + 回退路径实测（node 执行真实交付源码）
# ---------------------------------------------------------------------------

_NODE_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const grab = (re, name) => {
  const m = src.match(re);
  if (!m) { console.error(`MISSING SECTION: ${name}`); process.exit(2); }
  return m[0];
};
const transport = src.match(/\/\/ --- asa-floating-copilot-transport ---([\s\S]*?)\/\/ --- end asa-floating-copilot-transport ---/)[1];
const focusConsts = grab(/const FOCUS_ACTION_LABELS = \{[^}]*\};/, 'FOCUS_ACTION_LABELS');
const focusLabelFn = grab(/function focusBarLabel\(focus\)\{[\s\S]*?\n\}/, 'focusBarLabel');

const calls = [];
const warnings = [];
let v1ShouldFail = true;
globalThis.api = async (path, opts = {}) => {
  calls.push({ path, opts });
  if (path.includes('/api/v1/') && v1ShouldFail) throw new Error('HTTP 404');
  if (path.includes('/api/v1/')) return { ok: true, session_id: 'srv_session', answer: 'v1-answer', business_focus: { candidate: { name: '许尧' } } };
  return { ok: true, session_id: 'srv_session', answer: 'legacy-answer' };
};
console.warn = (...args) => warnings.push(args.join(' '));

const factory = new Function(`${focusConsts}\n${focusLabelFn}\n${transport}\nreturn { floatingMessageHash, floatingCopilotIdempotencyKey, floatingCopilotRequestId, postCopilotMessage, focusBarLabel };`);
const t = factory();

(async () => {
  const out = {};
  const vectors = ['', '可以搜索', '这个人选复核不通过', 'hello'];
  out.hashes = Object.fromEntries(vectors.map(v => [v, t.floatingMessageHash(v)]));
  out.hashStable = t.floatingMessageHash('这个人选复核不通过') === out.hashes['这个人选复核不通过'];
  out.hashFormat = vectors.every(v => /^[0-9a-z]+$/.test(out.hashes[v]));
  out.idemKey = t.floatingCopilotIdempotencyKey('floating_abc123', '可以搜索');
  out.requestId = t.floatingCopilotRequestId('floating_abc123', '可以搜索');

  calls.length = 0; warnings.length = 0; v1ShouldFail = true;
  const payload = { session_id: 'floating_abc123', message: '可以搜索', context: { type: 'job', id: 154 } };
  const r1 = await t.postCopilotMessage(payload, out.idemKey, out.requestId);
  out.fallback = {
    answer: r1.answer,
    callCount: calls.length,
    firstPath: calls[0].path,
    firstIdemHeader: calls[0].opts.headers['Idempotency-Key'],
    firstContentType: calls[0].opts.headers['Content-Type'],
    firstBody: JSON.parse(calls[0].opts.body),
    secondPath: calls[1] && calls[1].path,
    secondBody: calls[1] && JSON.parse(calls[1].opts.body),
    secondTimeoutMs: calls[1] && calls[1].opts.timeoutMs,
    warned: warnings.some(w => w.includes('/api/v1/copilot/messages') && w.includes('回退')),
  };

  calls.length = 0; warnings.length = 0; v1ShouldFail = false;
  const r2 = await t.postCopilotMessage(payload, out.idemKey, out.requestId);
  out.v1Success = { answer: r2.answer, callCount: calls.length, warned: warnings.length > 0, businessFocusName: r2.business_focus.candidate.name };

  out.focusLabel = {
    candidateFirst: t.focusBarLabel({ candidate: { name: '许尧' }, client: '士兰微', job: { title: 'AE 经理' } }),
    clientJob: t.focusBarLabel({ client: '士兰微', job: { title: 'AE 经理' } }),
    clientOnly: t.focusBarLabel({ client: '士兰微' }),
    empty: t.focusBarLabel({}),
    nullFocus: t.focusBarLabel(null),
    undefinedFocus: t.focusBarLabel(undefined),
  };
  console.log(JSON.stringify(out));
})().catch(err => { console.error('HARNESS ERROR', err); process.exit(1); });
"""


def _base36(value: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    out = ""
    while value:
        value, rem = divmod(value, 36)
        out = digits[rem] + out
    return out


def _reference_fnv1a_36(text: str) -> str:
    """浮窗 floatingMessageHash 的 Python 参考实现（UTF-16 code unit 版 FNV-1a）。"""
    data = text.encode("utf-16-le")
    value = 0x811C9DC5
    for i in range(0, len(data), 2):
        value ^= data[i] | (data[i + 1] << 8)
        value = (value * 0x01000193) & 0xFFFFFFFF
    return _base36(value)


@pytest.fixture(scope="module")
def transport_behavior() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用，跳过浮窗传输层 JS 行为测试")
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "harness.js"
        harness.write_text(_NODE_HARNESS, encoding="utf-8")
        proc = subprocess.run(
            [node, str(harness), str(SERVER_SOURCE)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_idempotency_key_derivation_matches_reference(transport_behavior: dict) -> None:
    expected = {v: _reference_fnv1a_36(v) for v in ["", "可以搜索", "这个人选复核不通过", "hello"]}
    assert transport_behavior["hashes"] == expected
    assert transport_behavior["hashStable"] is True
    assert transport_behavior["hashFormat"] is True
    session_hash = expected["可以搜索"]
    assert transport_behavior["idemKey"] == f"floating-floating_abc123-{session_hash}"
    assert transport_behavior["requestId"] == f"floating_req_floating_abc123_{session_hash}"


def test_v1_failure_falls_back_to_legacy_once(transport_behavior: dict) -> None:
    fallback = transport_behavior["fallback"]
    assert fallback["answer"] == "legacy-answer"
    assert fallback["callCount"] == 2
    assert fallback["firstPath"] == "/api/v1/copilot/messages"
    assert fallback["firstIdemHeader"] == transport_behavior["idemKey"]
    assert fallback["firstContentType"] == "application/json"
    assert fallback["firstBody"]["request_id"] == transport_behavior["requestId"]
    assert fallback["firstBody"]["message"] == "可以搜索"
    assert fallback["firstBody"]["session_id"] == "floating_abc123"
    # 回退打 legacy 一次，且请求体保持原始 payload（不带 request_id）
    assert fallback["secondPath"] == "/api/agent/copilot"
    assert "request_id" not in fallback["secondBody"]
    assert fallback["secondBody"]["message"] == "可以搜索"
    assert fallback["secondTimeoutMs"] == 45000
    # console 留痕
    assert fallback["warned"] is True


def test_v1_success_does_not_fallback(transport_behavior: dict) -> None:
    success = transport_behavior["v1Success"]
    assert success["answer"] == "v1-answer"
    assert success["callCount"] == 1
    assert success["warned"] is False
    assert success["businessFocusName"] == "许尧"


def test_focus_bar_label_semantics_match_react(transport_behavior: dict) -> None:
    labels = transport_behavior["focusLabel"]
    assert labels["candidateFirst"] == "许尧"
    assert labels["clientJob"] == "士兰微 / AE 经理"
    assert labels["clientOnly"] == "士兰微"
    assert labels["empty"] == ""
    assert labels["nullFocus"] == ""
    assert labels["undefinedFocus"] == ""
