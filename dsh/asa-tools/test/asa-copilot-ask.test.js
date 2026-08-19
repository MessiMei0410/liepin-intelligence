// asa_copilot_ask 工具层单测（node --test，本机跑；CI 暂无 dsh JS 测试门禁）。
// 验证：Copilot /api/v1/copilot/stream 的 done 事件原生携带 action_card 时，
// 工具返回值透传 action_card，且 presentationMeta 把卡片挂到 tool/result meta
// （常驻服务器据此向前端透传 SSE card 事件）。
import assert from "node:assert/strict";
import { describe, it, mock } from "node:test";

import { apply } from "../lib/index.js";

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
    assert.deepEqual(tool.output.presentationMeta({}, { answer: "x" }), { action_card: null });
    assert.deepEqual(tool.output.presentationMeta({}, { answer: "x", action_card: card }), { action_card: card });
  });
});
