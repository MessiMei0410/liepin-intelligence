// 写确认链路工具面单测（node --test，本机跑；CI 暂无 dsh JS 测试门禁）。
// 验证：
// - 模型工具面不再有 commit/decision/action 写工具（fail-closed）；
// - 三个 preflight 工具只调预检端点（POST body 不带 token 字段以外的写语义），
//   且 presentationMeta 把 confirm_request（preflight_token + 动作摘要 + 对象信息）
//   投影到 tool/result meta（常驻服务器据此透传 SSE confirm_request）；
// - 工具通道 UA 不含 ASAApp/ 前缀（Core 激活端点 UA 门拦得住模型通道）。
import assert from "node:assert/strict";
import { describe, it, mock } from "node:test";

import { apply } from "../lib/index.js";

function registerTools() {
  const tools = new Map();
  const ctx = { tools: { register: (definition) => tools.set(definition.name, definition) } };
  apply(ctx);
  return tools;
}

function stubFetchJson(payload, { ok = true, status = 200 } = {}) {
  return mock.method(globalThis, "fetch", async () => ({
    ok,
    status,
    json: async () => payload,
  }));
}

describe("写确认工具面（fail-closed）", () => {
  it("模型面不再有 commit/decision/action 写工具", () => {
    const tools = registerTools();
    for (const removed of ["asa_candidate_commit", "asa_approval_decision", "asa_workflow_action"]) {
      assert.equal(tools.has(removed), false, `${removed} 应从模型面移除`);
    }
    for (const kept of ["asa_candidate_preflight", "asa_approval_preflight", "asa_workflow_action_preflight"]) {
      assert.ok(tools.has(kept), `${kept} 应保留为预检申请工具`);
    }
  });

  it("工具通道 UA 不含 ASAApp/ 前缀（激活端点 UA 门拦得住）", async () => {
    const fetchMock = stubFetchJson({ ok: true, token: "tok-1" });
    try {
      const tool = registerTools().get("asa_candidate_preflight");
      await tool.execute({ candidate_id: 558, action: "advance" }, { signal: undefined });
      const [, init] = fetchMock.mock.calls[0].arguments;
      assert.ok(!String(init.headers["User-Agent"]).startsWith("ASAApp/"), `UA 不得为 ASAApp/ 前缀：${init.headers["User-Agent"]}`);
    } finally {
      fetchMock.mock.restore();
    }
  });
});

describe("asa_candidate_preflight", () => {
  it("调预检端点并把 confirm_request 投影到 presentationMeta", async () => {
    const preflight = {
      ok: true,
      token: "tok-abc",
      expires_at: "2026-08-19T12:00:00",
      action: "advance",
      candidate: { id: 558, name: "张桂芳", stage: "S1 新增寻访/待复核" },
      impact: "候选人关系状态将更新，并写入业务时间线和统一审计。",
    };
    const fetchMock = stubFetchJson(preflight);
    try {
      const tool = registerTools().get("asa_candidate_preflight");
      const result = await tool.execute({ candidate_id: 558, action: "advance" }, { signal: undefined });
      const [url, init] = fetchMock.mock.calls[0].arguments;
      assert.ok(String(url).endsWith("/api/v1/candidate-actions/preflight"));
      assert.equal(init.method, "POST");
      assert.deepEqual(result, preflight);
      const meta = tool.output.presentationMeta({}, result);
      assert.equal(meta.confirm_request.kind, "candidate_action");
      assert.equal(meta.confirm_request.preflight_token, "tok-abc");
      assert.equal(meta.confirm_request.expires_at, "2026-08-19T12:00:00");
      assert.equal(meta.confirm_request.action, "advance");
      assert.equal(meta.confirm_request.candidate.name, "张桂芳");
      assert.ok(meta.confirm_request.impact);
    } finally {
      fetchMock.mock.restore();
    }
  });
});

describe("asa_approval_preflight", () => {
  it("调 decision/preflight 并投影 approval_decision 确认请求", async () => {
    const preflight = {
      ok: true,
      token: "tok-ap",
      expires_at: "2026-08-19T12:00:00",
      action: "approval_decision:approve",
      note: "同意本轮寻访",
      approval: { approval_id: "approval_1", workflow_id: "workflow_1", title: "外部寻访审批", decision: "approve" },
      impact: "审批决定将写入工作流状态，并记入统一审计。",
    };
    const fetchMock = stubFetchJson(preflight);
    try {
      const tool = registerTools().get("asa_approval_preflight");
      const result = await tool.execute({ approval_id: "approval_1", decision: "approve", note: "同意本轮寻访" }, { signal: undefined });
      const [url] = fetchMock.mock.calls[0].arguments;
      assert.ok(String(url).endsWith("/api/v1/approvals/approval_1/decision/preflight"));
      const meta = tool.output.presentationMeta({}, result);
      assert.equal(meta.confirm_request.kind, "approval_decision");
      assert.equal(meta.confirm_request.preflight_token, "tok-ap");
      assert.equal(meta.confirm_request.approval.approval_id, "approval_1");
      assert.equal(meta.confirm_request.note, "同意本轮寻访");
    } finally {
      fetchMock.mock.restore();
    }
  });
});

describe("asa_workflow_action_preflight", () => {
  it("note 必填：空 note 不发请求直接报错", async () => {
    const tool = registerTools().get("asa_workflow_action_preflight");
    await assert.rejects(
      () => tool.execute({ workflow_id: "workflow_1", action: "pause", note: " " }, { signal: undefined }),
      /note 必填/,
    );
  });

  it("调 actions/preflight 并投影 workflow_action 确认请求", async () => {
    const preflight = {
      ok: true,
      token: "tok-wf",
      expires_at: "2026-08-19T12:00:00",
      action: "workflow_action:pause",
      note: "客户暂缓",
      workflow: { workflow_id: "workflow_1", status: "running", title: "寻访" },
      impact: "工作流状态将变更，并记入统一审计。",
    };
    const fetchMock = stubFetchJson(preflight);
    try {
      const tool = registerTools().get("asa_workflow_action_preflight");
      const result = await tool.execute({ workflow_id: "workflow_1", action: "pause", note: "客户暂缓" }, { signal: undefined });
      const [url] = fetchMock.mock.calls[0].arguments;
      assert.ok(String(url).endsWith("/api/v1/workflows/workflow_1/actions/preflight"));
      const meta = tool.output.presentationMeta({}, result);
      assert.equal(meta.confirm_request.kind, "workflow_action");
      assert.equal(meta.confirm_request.action, "pause");
      assert.equal(meta.confirm_request.workflow.workflow_id, "workflow_1");
      assert.equal(meta.confirm_request.note, "客户暂缓");
    } finally {
      fetchMock.mock.restore();
    }
  });
});
