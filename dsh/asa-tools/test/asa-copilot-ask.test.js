// asa_copilot_ask 工具层单测（node --test，本机跑；CI 暂无 dsh JS 测试门禁）。
// 验证：Copilot /api/v1/copilot/stream 的 done 事件原生携带 action_card 时，
// 工具返回值透传 action_card，且 presentationMeta 把卡片挂到 tool/result meta
// （常驻服务器据此向前端透传 SSE card 事件）；done 其余结构化字段（理解卡/
// 执行回执/工作流进度原料/焦点/模型参与/复数卡片/上下文）经 copilot_payload
// 投影，常驻服务器轮末并入 done；委托 session 派生自当前 DSH 会话（不再产生
// 一次性随机孤儿会话）。
import assert from "node:assert/strict";
import { describe, it, mock } from "node:test";

import { apply, delegateSessionId } from "../lib/index.js";

function registerTools() {
  const tools = new Map();
  const ctx = { tools: { register: (definition) => tools.set(definition.name, definition) } };
  apply(ctx);
  return tools;
}

function sseDone(payload) {
  return `event: text\ndata: {"content":"部分"}\n\nevent: done\ndata: ${JSON.stringify(payload)}\n\n`;
}

function stubFetchOnce(body, { ok = true, status = 200 } = {}) {
  return mock.method(globalThis, "fetch", async () => ({
    ok,
    status,
    text: async () => body,
  }));
}

describe("asa_copilot_ask", () => {
  it("done 事件携带 action_card 时透传到工具返回值", async () => {
    const card = { type: "candidate_list", context: { type: "job", id: 142 }, summary: { total: 7 }, filter_mode: "strict" };
    const fetchMock = stubFetchOnce(sseDone({
      answer: "名单如下",
      references: [{ type: "job", id: 142, label: "电源专家" }],
      suggested_actions: [],
      workflow_id: null,
      business_focus: null,
      action_card: card,
      action_cards: [card],
    }));
    try {
      const tool = registerTools().get("asa_copilot_ask");
      const result = await tool.execute({ message: "名单给我" }, { signal: undefined });
      assert.equal(result.answer, "名单如下");
      assert.deepEqual(result.action_card, card);
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("done 事件无 action_card 时返回 null，不阻塞答案", async () => {
    const fetchMock = stubFetchOnce(sseDone({ answer: "纯文本回答", references: [], suggested_actions: [] }));
    try {
      const tool = registerTools().get("asa_copilot_ask");
      const result = await tool.execute({ message: "你好" }, { signal: undefined });
      assert.equal(result.answer, "纯文本回答");
      assert.equal(result.action_card, null);
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("presentationMeta 把 action_card 挂到 meta（无卡时为 null，绝不返回 undefined）", async () => {
    const card = { type: "candidate_list", context: { type: "job", id: 142 }, summary: { total: 3 } };
    const tool = registerTools().get("asa_copilot_ask");
    assert.equal(typeof tool.output.presentationMeta, "function");
    // dsh-tools 对 presentationMeta 返回 undefined 会抛「non-lossless JSON」，必须总是对象。
    const empty = tool.output.presentationMeta({}, { answer: "x" });
    assert.equal(empty.action_card, null);
    assert.deepEqual(tool.output.presentationMeta({}, { answer: "x", action_card: card }).action_card, card);
  });

  it("done 结构化字段全量透传，presentationMeta 投影 copilot_payload", async () => {
    const card = { type: "candidate_list", summary: { total: 7 } };
    const done = {
      answer: "名单如下",
      references: [{ type: "job", id: 142, label: "电源专家" }],
      suggested_actions: [],
      action_card: card,
      understanding_card: { show: true, summary: "我理解为…" },
      execution_receipt: { state: "已生成建议" },
      workflow_id: "workflow_aaa",
      workflow: { status: "running", current_stage: "寻访中" },
      progress: { completed: 1, total: 4 },
      plan_summary: [{ title: "step1" }],
      approvals: [{ approval_id: "a1", status: "pending" }],
      goal: { business_outcome: "已产生推荐" },
      business_focus: { client: "士兰微" },
      model_participation: { mode: "model_tools", label: "模型生成 + 工具证据" },
      context: { type: "job", id: 142 },
    };
    let requestBody;
    const fetchMock = mock.method(globalThis, "fetch", async (_url, init) => {
      requestBody = JSON.parse(String(init.body));
      return { ok: true, status: 200, text: async () => sseDone(done) };
    });
    try {
      const tool = registerTools().get("asa_copilot_ask");
      const exec = { signal: undefined, agent: { id: "asa-777" } };
      const result = await tool.execute({ message: "名单给我", context: { type: "job", id: 142 } }, exec);
      // 委托 session 派生自 DSH 会话；委托轮次打标 source=dsh_delegate。
      assert.equal(requestBody.session_id, "asa-777::dsh-delegate");
      assert.equal(requestBody.context.source, "dsh_delegate");
      assert.equal(requestBody.context.type, "job");
      // 值透传：模型 render 文本可见全部字段。
      assert.equal(result.answer, "名单如下");
      assert.equal(result.workflow_id, "workflow_aaa");
      assert.deepEqual(result.understanding_card, done.understanding_card);
      assert.deepEqual(result.execution_receipt, done.execution_receipt);
      assert.deepEqual(result.business_focus, done.business_focus);
      assert.deepEqual(result.action_cards, [card]);
      // meta 投影：copilot_payload 是完整 JSON 快照（不受 render 16k 截断影响）。
      const meta = tool.output.presentationMeta({ message: "名单给我" }, result);
      assert.deepEqual(meta.action_card, card);
      assert.deepEqual(meta.copilot_payload, {
        understanding_card: done.understanding_card,
        execution_receipt: done.execution_receipt,
        workflow_id: "workflow_aaa",
        workflow: done.workflow,
        progress: done.progress,
        plan_summary: done.plan_summary,
        approvals: done.approvals,
        goal: done.goal,
        business_focus: done.business_focus,
        model_participation: done.model_participation,
        action_cards: [card],
        context: done.context,
      });
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("done 只有 answer 时投影安全兜底（全 null/空，绝不返回 undefined）", async () => {
    const fetchMock = stubFetchOnce(sseDone({ answer: "纯文本回答", references: [], suggested_actions: [] }));
    try {
      const tool = registerTools().get("asa_copilot_ask");
      const result = await tool.execute({ message: "你好" }, { signal: undefined });
      const meta = tool.output.presentationMeta({}, result);
      assert.equal(meta.action_card, null);
      assert.equal(meta.copilot_payload.workflow_id, null);
      assert.equal(meta.copilot_payload.understanding_card, null);
      assert.deepEqual(meta.copilot_payload.action_cards, []);
    } finally {
      fetchMock.mock.restore();
    }
  });
});

describe("delegateSessionId", () => {
  it("派生自当前 DSH 会话 id（固定委托 session，同会话多次委托共享上下文）", () => {
    assert.equal(delegateSessionId({ agent: { id: "asa-123" } }), "asa-123::dsh-delegate");
  });

  it("无 agent 上下文时回退随机 dsh- 前缀（Core rollup 同样过滤该前缀）", () => {
    assert.match(delegateSessionId({}), /^dsh-[0-9a-f-]+$/);
    assert.match(delegateSessionId(undefined), /^dsh-[0-9a-f-]+$/);
  });
});
