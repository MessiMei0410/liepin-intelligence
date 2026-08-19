// asa-server 流式聚合 + thinking 透传单测（node --test，本机跑；CI 暂无 dsh JS 测试门禁）。
// 验证：text/thinking 增量按 maxChars 立即 flush、按 windowMs 定时 flush、手动 flush
// 保序且幂等；reasoning-delta → thinking 事件映射；tool/call 与 tool/result 进度文案。
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  chunkSseDelta,
  createDeltaAggregator,
  toolCallProgressMessage,
  TOOL_RESULT_PROGRESS_MESSAGE,
} from "../lib/index.js";

describe("chunkSseDelta", () => {
  it("text-delta → text 事件，reasoning-delta → thinking 事件", () => {
    assert.deepEqual(chunkSseDelta({ type: "text-delta", index: 0, text: "你好" }), ["text", "你好"]);
    assert.deepEqual(chunkSseDelta({ type: "reasoning-delta", index: 0, text: "先分析岗位" }), ["thinking", "先分析岗位"]);
  });

  it("其他 chunk 与空文本不透传", () => {
    assert.equal(chunkSseDelta({ type: "tool-call-delta", index: 1, text: "{}" }), null);
    assert.equal(chunkSseDelta({ type: "block-start", index: 0, blockType: "text" }), null);
    assert.equal(chunkSseDelta({ type: "text-delta", index: 0, text: "" }), null);
    assert.equal(chunkSseDelta(undefined), null);
    assert.equal(chunkSseDelta(null), null);
  });
});

describe("createDeltaAggregator", () => {
  it("未达阈值不发事件，windowMs 到点 flush 一次", async () => {
    const writes = [];
    const agg = createDeltaAggregator((type, data) => writes.push([type, data]), { windowMs: 20, maxChars: 100 });
    agg.push("text", "你");
    agg.push("text", "好");
    assert.deepEqual(writes, []);
    await new Promise((resolve) => setTimeout(resolve, 60));
    assert.deepEqual(writes, [["text", { content: "你好" }]]);
    // 定时器只触发一次，不重复发
    await new Promise((resolve) => setTimeout(resolve, 60));
    assert.equal(writes.length, 1);
  });

  it("累计满 maxChars 立即 flush 该类型，不等定时器", () => {
    const writes = [];
    const agg = createDeltaAggregator((type, data) => writes.push([type, data]), { windowMs: 10_000, maxChars: 4 });
    agg.push("text", "ab");
    assert.deepEqual(writes, []);
    agg.push("text", "cd");
    assert.deepEqual(writes, [["text", { content: "abcd" }]]);
  });

  it("text 与 thinking 各自累积，flush 全部写出且保持类型分离", () => {
    const writes = [];
    const agg = createDeltaAggregator((type, data) => writes.push([type, data]), { windowMs: 10_000, maxChars: 100 });
    agg.push("thinking", "推理");
    agg.push("text", "答案");
    agg.flush();
    assert.deepEqual(writes, [["text", { content: "答案" }], ["thinking", { content: "推理" }]]);
    // flush 幂等：再 flush 不再写
    agg.flush();
    assert.equal(writes.length, 2);
  });

  it("flush 后取消未触发定时器（轮末 flush 不再二次写）", async () => {
    const writes = [];
    const agg = createDeltaAggregator((type, data) => writes.push([type, data]), { windowMs: 20, maxChars: 100 });
    agg.push("text", "残留");
    agg.flush();
    await new Promise((resolve) => setTimeout(resolve, 60));
    assert.deepEqual(writes, [["text", { content: "残留" }]]);
  });
});

describe("工具进度文案", () => {
  it("tool/call 用已知工具标签，未知工具回退工具名", () => {
    assert.equal(toolCallProgressMessage("asa_candidates"), "查询候选人…");
    assert.equal(toolCallProgressMessage("asa_copilot_ask"), "委托 Copilot 做领域分析…");
    assert.equal(toolCallProgressMessage("some_other_tool"), "调用工具 some_other_tool…");
  });

  it("tool/result 后补生成中进度", () => {
    assert.equal(TOOL_RESULT_PROGRESS_MESSAGE, "整理工具结果，生成中…");
  });
});
