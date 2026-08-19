// asa_copilot_ask 流式读取单测（dogfood P1-1：名单/策略类委托富答案生成 2-4 分钟，
// 旧的整流缓冲 + 120s 绝对超时导致连续超时）。验证：
// - done 块完整到达即返回，不等连接收尾（流式提前返回）；
// - 静默看门狗：超过 INACTIVITY_MS 没有任何字节才判卡死；
// - 超时预算上调（280s，300s 轮预算内一次完整尝试）。
import assert from "node:assert/strict";
import { describe, it, mock } from "node:test";

process.env.ASA_COPILOT_ASK_INACTIVITY_MS = process.env.ASA_COPILOT_ASK_INACTIVITY_MS || "60000";

const { apply } = await import("../lib/index.js");

function registerTools() {
  const tools = new Map();
  const ctx = { tools: { register: (definition) => tools.set(definition.name, definition) } };
  apply(ctx);
  return tools;
}

function sseResponse(chunks, { hangAfter = false } = {}) {
  const encoder = new TextEncoder();
  let index = 0;
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: async () => {
          if (index < chunks.length) {
            const value = encoder.encode(chunks[index]);
            index += 1;
            return { value, done: false };
          }
          if (hangAfter) return new Promise(() => {}); // 连接不收尾（服务器保持）
          return { value: undefined, done: true };
        },
        cancel: async () => {},
      }),
    },
  };
}

describe("asa_copilot_ask 流式读取", () => {
  it("timeoutMs 上调到 280s（300s 轮预算内一次完整尝试）", () => {
    const tool = registerTools().get("asa_copilot_ask");
    assert.equal(tool.timeoutMs, 280000);
  });

  it("done 块完整到达即返回，不等连接收尾", async () => {
    const done = { answer: "完整富答案", references: [] };
    const fetchMock = mock.method(globalThis, "fetch", async () =>
      sseResponse([
        'event: text\ndata: {"content":"部分"}\n\n',
        `event: done\ndata: ${JSON.stringify(done)}\n\n`,
      ], { hangAfter: true }));
    try {
      const tool = registerTools().get("asa_copilot_ask");
      const result = await tool.execute({ message: "名单给我" }, { signal: undefined });
      assert.equal(result.answer, "完整富答案");
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("done 数据跨 chunk 时不提前返回（等数据完整）", async () => {
    const payload = JSON.stringify({ answer: "跨块答案", references: [] });
    const cut = Math.floor(payload.length / 2);
    const fetchMock = mock.method(globalThis, "fetch", async () =>
      sseResponse([
        `event: done\ndata: ${payload.slice(0, cut)}`,
        `${payload.slice(cut)}\n\n`,
      ], { hangAfter: true }));
    try {
      const tool = registerTools().get("asa_copilot_ask");
      const result = await tool.execute({ message: "名单给我" }, { signal: undefined });
      assert.equal(result.answer, "跨块答案");
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("静默流触发看门狗报卡死", async () => {
    process.env.ASA_COPILOT_ASK_INACTIVITY_MS = "50";
    const { apply: applyShort } = await import(`../lib/index.js?inactivity=50`);
    const tools = new Map();
    applyShort({ tools: { register: (definition) => tools.set(definition.name, definition) } });
    const fetchMock = mock.method(globalThis, "fetch", async () => sseResponse([], { hangAfter: true }));
    try {
      const tool = tools.get("asa_copilot_ask");
      await assert.rejects(() => tool.execute({ message: "你好" }, { signal: undefined }), /静默/);
    } finally {
      fetchMock.mock.restore();
      process.env.ASA_COPILOT_ASK_INACTIVITY_MS = "60000";
    }
  });

  it("无流式 body 的测试桩/旧环境退回整体文本", async () => {
    const done = { answer: "文本兜底", references: [] };
    const fetchMock = mock.method(globalThis, "fetch", async () => ({
      ok: true,
      status: 200,
      text: async () => `event: done\ndata: ${JSON.stringify(done)}\n\n`,
    }));
    try {
      const tool = registerTools().get("asa_copilot_ask");
      const result = await tool.execute({ message: "你好" }, { signal: undefined });
      assert.equal(result.answer, "文本兜底");
    } finally {
      fetchMock.mock.restore();
    }
  });
});
