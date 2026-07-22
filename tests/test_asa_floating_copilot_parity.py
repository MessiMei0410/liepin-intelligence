"""R12-a/R12-b 浮窗 parity 补齐的回归守护。

覆盖浮窗侧改动（均为 additive，legacy 语义不变）：
1. business_focus 焦点条——浮窗渲染每条 copilot 响应与
   /api/agent/copilot/session 恢复响应里的 business_focus 字段，
   含 needs_clarification 冲突态（语义对齐 React Copilot 焦点卡）。
2. 发送切 v1——sendMessage 经 postCopilotMessage 打
   /api/v1/copilot/messages，Idempotency-Key=floating-{sessionId}-{messageHash}
   （FNV-1a 32 位稳定散列），request_id 同理派生；v1 失败回退
   legacy /api/agent/copilot 一次并 console 留痕。
3. R9 确认卡（R12-b）——响应/恢复消息携带 pending_intent 时在对应消息下
   渲染确认卡（confirm_text + 候选人摘要 + 确认/取消），确认打
   /api/v1/copilot/intents/confirm（Idempotency-Key=
   floating-confirm-{sessionId}-{intentHash 前 12 位}），四态语义
   （pending/confirmed/cancelled/drift）对齐已下线的 React IntentCard；
   取消零写请求，409 红框展示服务端 detail，终态幂等防双击。

幂等 key 派生函数与确认卡状态机的单测通过 node 真实执行浮窗 HTML 里的 JS
完成（tests 下既有 asa_floating_attachment_ui.spec.js 同款思路：直接验证
交付给浏览器的实际源码，而不是 Python 侧的重写版）。
"""

from __future__ import annotations

import json
import re
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


# ---------------------------------------------------------------------------
# 4. R9 确认卡（R12-b）：源码契约
# ---------------------------------------------------------------------------


def _floating_html_source() -> str:
    return _server_text().split("def asa_floating_html() -> str:", 1)[1].split("\ndef ", 1)[0]


def _intent_confirm_section() -> str:
    return _server_text().split("// --- asa-floating-copilot-intent-confirm ---", 1)[1].split(
        "// --- end asa-floating-copilot-intent-confirm ---", 1
    )[0]


def test_floating_intent_card_rendering_contract() -> None:
    source = _server_text()
    for marker in [
        "// --- asa-floating-copilot-intent-confirm ---",
        "// --- end asa-floating-copilot-intent-confirm ---",
        ".intent-card.drift",
        ".intent-card-actions",
        "renderFloatingIntentCard",
        "floatingIntentCandidateLine",
        "confirmFloatingIntent",
        "cancelFloatingIntent",
        "data-intent-confirm",
        "data-intent-cancel",
        "message.intentCard",
        "intent.confirm_text || '该操作需要确认后才会执行。'",
        "已确认，操作已执行。",
        "已取消，未执行任何写操作。",
        "候选人状态已变化，意图签名不再有效，请重新发起指令。",
        "state.intentConfirmBusy",
    ]:
        assert marker in source, marker
    # 候选人摘要格式「姓名 · 阶段 / 客户 · 岗位」
    assert "[candidate.name, candidate.stage].filter(Boolean).join(' · ')" in source
    assert "[candidate.client, candidate.job].filter(Boolean).join(' · ')" in source
    # 零 JS 原生对话框（WKWebView 不实现对话框代理，与仓库B硬性约定一致）；
    # confirmFloatingIntent 等自定义函数名不含 "confirm(" 调用形态。
    assert not re.search(r"\b(?:window\.)?confirm\s*\(", _floating_html_source())


def test_floating_intent_confirm_transport_contract() -> None:
    section = _intent_confirm_section()
    # 唯一的确认写出口：v1 确认端点，仅出现一次；不直连 legacy 做确认
    assert section.count("fetch('/api/v1/copilot/intents/confirm'") == 1
    assert "/api/agent/copilot" not in section
    assert "'Idempotency-Key':floatingIntentConfirmIdempotencyKey(sessionId, intentHash)" in section
    # 幂等键规格：floating-confirm-{sessionId}-{intentHash 前 12 位}；request_id 同理派生
    assert "return `floating-confirm-${sessionId}-${String(intentHash || '').slice(0, 12)}`;" in section
    assert "return `floating_confirm_req_${sessionId}_${String(intentHash || '').slice(0, 12)}`;" in section
    # 提交体契约字段（WriteEnvelope.request_id + CopilotIntentConfirm 全字段）
    for field in ["request_id:", "intent_hash:", "candidate_id:", "preflight_token:", "message:", "session_id:", "kind:", "action:"]:
        assert field in section, field
    # 409 的服务端 detail 必须可读展示；error.status 供调用方按状态分流
    assert "json.detail" in section
    assert "error.status" in section


def test_floating_intent_card_data_wiring_contract() -> None:
    source = _server_text()
    send_message = source.split("async function sendMessage()", 1)[1].split("\nloadState();", 1)[0]
    # sendMessage 响应的 pending_intent 挂到 assistant 消息（仅 intent_hash 存在时，对齐 React 判定）
    assert "pending_intent: result.pending_intent && result.pending_intent.intent_hash ? result.pending_intent : null" in send_message
    # 渲染挂进消息流：renderFloatingMessage 追加卡片，renderMessages 接线按钮事件
    assert "renderFloatingIntentCard(message, index)" in source
    assert "state.messages.map(renderFloatingMessage)" in source
    assert "confirmFloatingIntent(Number(button.dataset.intentConfirm))" in source
    assert "cancelFloatingIntent(Number(button.dataset.intentCancel))" in source
    # 恢复链：get_copilot_session 透传持久化的 pending_intent，
    # restoreCurrentSession/loadSession 经统一 renderMessages 重渲染出卡
    agent_service = (ROOT / "scripts" / "a_system_agent" / "service.py").read_text(encoding="utf-8")
    assert '"pending_intent": structured.get("pending_intent"),' in agent_service


# ---------------------------------------------------------------------------
# 5. R9 确认卡：node 行为测试（执行真实交付源码，stub fetch/document/state）
# ---------------------------------------------------------------------------

_INTENT_NODE_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const section = src.match(/\/\/ --- asa-floating-copilot-intent-confirm ---([\s\S]*?)\/\/ --- end asa-floating-copilot-intent-confirm ---/)[1];
const escFn = src.match(/function esc\(v\)\{[^\n]*\}/)[0];

const INTENT = {
  kind: 'candidate_action', action: 'stop', action_label: '停止推进',
  target_scope: 'current_candidate', confidence: 0.91, reason: '用户明确要求停止推进',
  candidate: { id: 7, name: '张三', stage: 'S1 待复核', client: 'ACME', job: '前端工程师' },
  confirm_text: '将停止推进 张三，确认？',
  intent_hash: 'hash-abc123def4567890',
  preflight_token: 'tok-xyz',
  expires_at: '2026-07-22 11:00',
  message: '停止推进张三',
};

const calls = [];
let fetchMode = 'success';
globalThis.fetch = async (url, opts = {}) => {
  calls.push({ url: String(url), opts });
  if (fetchMode === 'success') return { ok: true, status: 200, json: async () => ({ ok: true, candidate_action: { action: 'stop' }, answer: '已确认并同步到 ASA：张三 停止推进。' }) };
  if (fetchMode === 'drift') return { ok: false, status: 409, json: async () => ({ detail: '该人选已经停止推进，不能重复停止。' }) };
  if (fetchMode === 'server-error') return { ok: false, status: 500, json: async () => ({ detail: 'boom' }) };
  throw new Error(`unexpected fetchMode ${fetchMode}`);
};
const statusNode = { textContent: '' };
globalThis.document = { getElementById: () => statusNode };
let renderCount = 0;
globalThis.renderMessages = () => { renderCount += 1; };
globalThis.state = { sessionId: 'floating_test123', messages: [], intentConfirmBusy: false };

const factory = new Function(`${escFn}\n${section}\nreturn { floatingIntentConfirmIdempotencyKey, floatingIntentConfirmRequestId, floatingIntentCandidateLine, renderFloatingIntentCard, confirmFloatingIntent, cancelFloatingIntent };`);
const t = factory();

const freshCard = () => {
  state.messages = [{ role: 'assistant', content: `${INTENT.confirm_text}\n\n未确认前不会写入 ASA。`, pending_intent: INTENT }];
  state.intentConfirmBusy = false;
  statusNode.textContent = '';
};

(async () => {
  const out = {};
  out.idemKey = t.floatingIntentConfirmIdempotencyKey('floating_test123', INTENT.intent_hash);
  out.requestId = t.floatingIntentConfirmRequestId('floating_test123', INTENT.intent_hash);
  out.candidateLine = {
    full: t.floatingIntentCandidateLine(INTENT),
    noStage: t.floatingIntentCandidateLine({ candidate: { name: '张三', client: 'ACME', job: '前端工程师' } }),
    noCandidate: t.floatingIntentCandidateLine({}),
  };
  // 卡片渲染：pending 含确认/取消按钮；busy 时按钮 disabled；无 intent_hash 不渲染
  freshCard();
  out.cardHtml = {
    pending: t.renderFloatingIntentCard(state.messages[0], 0),
    noHash: t.renderFloatingIntentCard({ role: 'assistant', content: 'x', pending_intent: { kind: 'candidate_action' } }, 0),
    noIntent: t.renderFloatingIntentCard({ role: 'assistant', content: 'x' }, 0),
  };
  state.intentConfirmBusy = true;
  out.cardHtml.busy = t.renderFloatingIntentCard(state.messages[0], 0);
  state.intentConfirmBusy = false;

  // 确认成功：提交体逐字段 + answer 追加 + confirmed 终态；终态后再点零新请求
  freshCard(); calls.length = 0; renderCount = 0; fetchMode = 'success';
  await t.confirmFloatingIntent(0);
  out.confirm = {
    callCount: calls.length,
    url: calls[0] && calls[0].url,
    method: calls[0] && calls[0].opts.method,
    idemHeader: calls[0] && calls[0].opts.headers['Idempotency-Key'],
    contentType: calls[0] && calls[0].opts.headers['Content-Type'],
    body: calls[0] && JSON.parse(calls[0].opts.body),
    cardState: state.messages[0].intentCard.state,
    appended: state.messages[1] && state.messages[1].content,
    statusText: statusNode.textContent,
    renders: renderCount,
  };
  await t.confirmFloatingIntent(0);
  out.confirm.afterTerminalExtraCalls = calls.length - out.confirm.callCount;

  // 防双击：busy 期间第二次点击不发请求
  freshCard(); calls.length = 0; fetchMode = 'success';
  await Promise.all([t.confirmFloatingIntent(0), t.confirmFloatingIntent(0)]);
  out.doubleClick = { callCount: calls.length, cardState: state.messages[0].intentCard.state };

  // 取消：零写请求 + cancelled 终态文案
  freshCard(); calls.length = 0; renderCount = 0;
  t.cancelFloatingIntent(0);
  out.cancel = { callCount: calls.length, cardState: state.messages[0].intentCard.state, renders: renderCount, html: t.renderFloatingIntentCard(state.messages[0], 0) };

  // 409：drift 终态 + 红框 + 服务端 detail；不追加消息
  freshCard(); calls.length = 0; fetchMode = 'drift';
  statusNode.textContent = 'ASA 正在处理...';
  await t.confirmFloatingIntent(0);
  out.drift = {
    callCount: calls.length,
    cardState: state.messages[0].intentCard.state,
    error: state.messages[0].intentCard.error,
    messageCount: state.messages.length,
    html: t.renderFloatingIntentCard(state.messages[0], 0),
  };

  // 其他错误：走 chatStatus 现有错误模式，卡片保持 pending
  freshCard(); calls.length = 0; fetchMode = 'server-error';
  await t.confirmFloatingIntent(0);
  out.otherError = { cardState: state.messages[0].intentCard.state, statusText: statusNode.textContent, messageCount: state.messages.length };

  console.log(JSON.stringify(out));
})().catch(err => { console.error('HARNESS ERROR', err); process.exit(1); });
"""


@pytest.fixture(scope="module")
def intent_behavior() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用，跳过浮窗确认卡 JS 行为测试")
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "harness.js"
        harness.write_text(_INTENT_NODE_HARNESS, encoding="utf-8")
        proc = subprocess.run(
            [node, str(harness), str(SERVER_SOURCE)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_intent_confirm_key_derivation(intent_behavior: dict) -> None:
    # intent_hash 'hash-abc123def4567890' 前 12 位 = 'hash-abc123d'
    assert intent_behavior["idemKey"] == "floating-confirm-floating_test123-hash-abc123d"
    assert intent_behavior["requestId"] == "floating_confirm_req_floating_test123_hash-abc123d"


def test_intent_candidate_line_format(intent_behavior: dict) -> None:
    lines = intent_behavior["candidateLine"]
    assert lines["full"] == "张三 · S1 待复核 / ACME · 前端工程师"
    assert lines["noStage"] == "张三 / ACME · 前端工程师"
    assert lines["noCandidate"] == ""


def test_intent_card_rendering_states(intent_behavior: dict) -> None:
    html = intent_behavior["cardHtml"]
    assert 'intent-card pending' in html["pending"]
    assert "将停止推进 张三，确认？" in html["pending"]
    assert "张三 · S1 待复核 / ACME · 前端工程师" in html["pending"]
    assert 'data-intent-confirm="0"' in html["pending"]
    assert 'data-intent-cancel="0"' in html["pending"]
    assert "disabled" not in html["pending"]
    assert html["busy"].count("disabled") == 2
    # 无 intent_hash / 无 pending_intent 时不渲染卡片
    assert html["noHash"] == ""
    assert html["noIntent"] == ""


def test_intent_confirm_success_body_and_terminal(intent_behavior: dict) -> None:
    confirm = intent_behavior["confirm"]
    assert confirm["callCount"] == 1
    assert confirm["url"] == "/api/v1/copilot/intents/confirm"
    assert confirm["method"] == "POST"
    assert confirm["idemHeader"] == intent_behavior["idemKey"]
    assert confirm["contentType"] == "application/json"
    # 提交体逐字段（intent_hash/preflight_token/message 原样回传）
    assert confirm["body"] == {
        "request_id": intent_behavior["requestId"],
        "intent": {"kind": "candidate_action", "action": "stop"},
        "intent_hash": "hash-abc123def4567890",
        "candidate_id": 7,
        "preflight_token": "tok-xyz",
        "message": "停止推进张三",
        "session_id": "floating_test123",
    }
    assert confirm["cardState"] == "confirmed"
    assert confirm["appended"] == "已确认并同步到 ASA：张三 停止推进。"
    assert confirm["statusText"] == ""
    assert confirm["renders"] == 2
    # 终态幂等：confirmed 后再点确认零新请求
    assert confirm["afterTerminalExtraCalls"] == 0


def test_intent_confirm_double_click_single_request(intent_behavior: dict) -> None:
    double_click = intent_behavior["doubleClick"]
    assert double_click["callCount"] == 1
    assert double_click["cardState"] == "confirmed"


def test_intent_cancel_zero_write(intent_behavior: dict) -> None:
    cancel = intent_behavior["cancel"]
    assert cancel["callCount"] == 0
    assert cancel["cardState"] == "cancelled"
    assert cancel["renders"] == 1
    assert "intent-card cancelled" in cancel["html"]
    assert "已取消，未执行任何写操作。" in cancel["html"]
    assert "data-intent-confirm" not in cancel["html"]


def test_intent_confirm_409_drift(intent_behavior: dict) -> None:
    drift = intent_behavior["drift"]
    assert drift["callCount"] == 1
    assert drift["cardState"] == "drift"
    assert drift["error"] == "该人选已经停止推进，不能重复停止。"
    # 不追加新消息；卡片红框 + 服务端 detail 原文
    assert drift["messageCount"] == 1
    assert "intent-card drift" in drift["html"]
    assert "该人选已经停止推进，不能重复停止。" in drift["html"]
    assert "data-intent-confirm" not in drift["html"]


def test_intent_confirm_other_error_uses_chatstatus(intent_behavior: dict) -> None:
    other = intent_behavior["otherError"]
    assert other["cardState"] == "pending"
    assert other["statusText"] == "boom"
    assert other["messageCount"] == 1
