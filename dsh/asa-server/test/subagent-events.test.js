// asa-server 子代理生命周期 → SSE `subagent` 透传单测（node --test，本机跑；CI 暂无 dsh JS 测试门禁）。
// 验证：subagent/start|end → 增量事件形态（start 带 label/status=running，end 带终态+摘要）、
// stopReason → 状态词汇映射、descriptor label 优先于 tool/call 描述兜底、轮末终态数组聚合。
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  createSubagentTracker,
  isSubagentToolCall,
  subagentSummaryText,
  subagentTerminalStatus,
  subagentToolCallLabel,
} from "../lib/subagent-events.js";

describe("subagentTerminalStatus", () => {
  it("completed→done、error/max-tokens/refusal→failed、aborted→stopped、未知→failed", () => {
    assert.equal(subagentTerminalStatus("completed"), "done");
    assert.equal(subagentTerminalStatus("error"), "failed");
    assert.equal(subagentTerminalStatus("max-tokens"), "failed");
    assert.equal(subagentTerminalStatus("refusal"), "failed");
    assert.equal(subagentTerminalStatus("aborted"), "stopped");
    assert.equal(subagentTerminalStatus("something-new"), "failed");
    assert.equal(subagentTerminalStatus(undefined), "failed");
  });
});

describe("subagentSummaryText", () => {
  it("拼接 text block 并去空白", () => {
    const blocks = [{ type: "text", text: "结论：" }, { type: "tool_use", id: "x" }, { type: "text", text: "已核实 3 人。" }];
    assert.equal(subagentSummaryText(blocks), "结论：已核实 3 人。");
  });

  it("非数组/空文本返回空串；超长截断", () => {
    assert.equal(subagentSummaryText(undefined), "");
    assert.equal(subagentSummaryText([{ type: "text", text: "   " }]), "");
    const long = subagentSummaryText([{ type: "text", text: "a".repeat(600) }]);
    assert.equal(long.length, 501);
    assert.ok(long.endsWith("…"));
  });
});

describe("subagentToolCallLabel / isSubagentToolCall", () => {
  it("subagent 与 subagent_fork 都算委派工具", () => {
    assert.equal(isSubagentToolCall("subagent"), true);
    assert.equal(isSubagentToolCall("subagent_fork"), true);
    assert.equal(isSubagentToolCall("asa_jobs"), false);
  });

  it("从 arguments JSON 提取 description；坏 JSON/缺字段返回空串", () => {
    assert.equal(subagentToolCallLabel('{"description":"核实候选人近况","prompt":"..."}'), "核实候选人近况");
    assert.equal(subagentToolCallLabel('{"prompt":"..."}'), "");
    assert.equal(subagentToolCallLabel("not-json"), "");
    assert.equal(subagentToolCallLabel(undefined), "");
  });
});

describe("createSubagentTracker", () => {
  it("start 用 descriptor label 优先，SSE data 为 start 增量形态", () => {
    const tracker = createSubagentTracker();
    const data = tracker.start({ runId: "run-1", id: "child-1", provider: "spawn", local: true }, "调研竞品岗位");
    assert.deepEqual(data, { event: "start", id: "run-1", label: "调研竞品岗位", status: "running" });
    assert.deepEqual(tracker.list(), [{ id: "run-1", label: "调研竞品岗位", status: "running" }]);
  });

  it("descriptor label 缺失时按序退到 tool/call 描述兜底", () => {
    const tracker = createSubagentTracker();
    tracker.noteToolCall("subagent", '{"description":"背调甲","prompt":"p1"}');
    tracker.noteToolCall("subagent_fork", '{"description":"背调乙","prompt":"p2"}');
    tracker.noteToolCall("asa_jobs", "{}"); // 非委派工具不进队列
    const first = tracker.start({ runId: "run-1", id: "c1", provider: "spawn", local: true }, "");
    const second = tracker.start({ runId: "run-2", id: "c2", provider: "fork", local: true }, null);
    assert.equal(first.label, "背调甲");
    assert.equal(second.label, "背调乙");
  });

  it("end 映射终态并带摘要；list() 输出终态数组", () => {
    const tracker = createSubagentTracker();
    tracker.start({ runId: "run-1", id: "c1", provider: "spawn", local: true }, "背调甲");
    tracker.start({ runId: "run-2", id: "c2", provider: "spawn", local: true }, "背调乙");
    const endData = tracker.end({
      runId: "run-1",
      id: "c1",
      provider: "spawn",
      local: true,
      stopReason: "completed",
      lastAssistantMessage: [{ type: "text", text: "甲已核实，在职。" }],
    });
    assert.deepEqual(endData, { event: "end", id: "run-1", status: "done", summary: "甲已核实，在职。" });
    assert.deepEqual(tracker.list(), [
      { id: "run-1", label: "背调甲", status: "done", summary: "甲已核实，在职。" },
      { id: "run-2", label: "背调乙", status: "running" },
    ]);
  });

  it("无摘要的 end 不带 summary 键；未知 runId 的 end 兜底成终态行", () => {
    const tracker = createSubagentTracker();
    tracker.start({ runId: "run-1", id: "c1", provider: "spawn", local: true }, "背调甲");
    assert.deepEqual(tracker.end({ runId: "run-1", id: "c1", provider: "spawn", local: true, stopReason: "aborted" }), {
      event: "end",
      id: "run-1",
      status: "stopped",
    });
    // 孤儿 end（start 事件丢失时）也要落成终态行，不丢事件。
    assert.deepEqual(tracker.end({ runId: "run-9", id: "c9", provider: "spawn", local: true, stopReason: "error" }), {
      event: "end",
      id: "run-9",
      status: "failed",
    });
    assert.deepEqual(tracker.list(), [
      { id: "run-1", label: "背调甲", status: "stopped" },
      { id: "run-9", label: "", status: "failed" },
    ]);
  });

  it("缺 runId 的畸形事件返回 null", () => {
    const tracker = createSubagentTracker();
    assert.equal(tracker.start(undefined), null);
    assert.equal(tracker.start({}), null);
    assert.equal(tracker.end(null), null);
    assert.deepEqual(tracker.list(), []);
  });
});
