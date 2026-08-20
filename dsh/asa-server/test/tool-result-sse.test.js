// asa-server tool/result → SSE 透传单测（node --test，本机跑；CI 暂无 dsh JS 测试门禁）。
// 验证：presentationMeta 里的 action_card → card 事件、confirm_request → confirm_request
// 事件（必须带 preflight_token 才算有效），其余 meta 不透传。
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { toolResultSseEvents } from "../lib/index.js";

describe("toolResultSseEvents", () => {
  it("action_card 透传为 card 事件", () => {
    const card = { type: "candidate_list", summary: { total: 3 } };
    assert.deepEqual(toolResultSseEvents({ meta: { action_card: card } }), [["card", card]]);
  });

  it("confirm_request 透传为 confirm_request 事件", () => {
    const confirm = { kind: "candidate_action", preflight_token: "tok-1", action: "advance", candidate: { id: 558 } };
    assert.deepEqual(toolResultSseEvents({ meta: { confirm_request: confirm } }), [["confirm_request", confirm]]);
  });

  it("缺 preflight_token 的 confirm_request 不透传（防半截投影触发确认卡）", () => {
    assert.deepEqual(toolResultSseEvents({ meta: { confirm_request: { kind: "candidate_action" } } }), []);
  });

  it("filter_note_batch 批量确认请求带 items 数组无损透传", () => {
    const confirm = {
      kind: "filter_note_batch",
      preflight_token: "tok-batch-1",
      action: "job_filter_note_batch",
      items: [
        { job_id: 137, job: { id: 137, title: "机械高级工程师", client: "长越科技" }, note: "口径 A", previous_note: "" },
        { job_id: 138, job: { id: 138, title: "软件高级工程师", client: "长越科技" }, note: "口径 B", previous_note: "旧" },
      ],
    };
    assert.deepEqual(toolResultSseEvents({ meta: { confirm_request: confirm } }), [["confirm_request", confirm]]);
  });

  it("card 与 confirm_request 可同时透传；空 meta 零事件", () => {
    const card = { type: "candidate_list" };
    const confirm = { kind: "workflow_action", preflight_token: "tok-2", action: "pause" };
    assert.deepEqual(toolResultSseEvents({ meta: { action_card: card, confirm_request: confirm } }), [
      ["card", card],
      ["confirm_request", confirm],
    ]);
    assert.deepEqual(toolResultSseEvents({}), []);
    assert.deepEqual(toolResultSseEvents(undefined), []);
  });
});
