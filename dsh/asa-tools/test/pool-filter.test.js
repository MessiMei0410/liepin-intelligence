// asa_pool_filter 工具单测（node --test，本机跑；CI 暂无 dsh JS 测试门禁）。
// 验证：参数透传（job_id/filter_mode/bonder → POST body）、{answer, card} 返回形态、
// presentationMeta 把 card 投影为 meta.action_card（常驻服务器据此透传 SSE card →
// 前端名单弹窗）+ object_refs 岗位引用投影、错误路径（非法 job_id / Core 非 2xx）。
import assert from "node:assert/strict";
import { describe, it, mock } from "node:test";

import { apply } from "../lib/index.js";

function registerTools() {
  const tools = new Map();
  const ctx = { tools: { register: (definition) => tools.set(definition.name, definition) } };
  apply(ctx);
  return tools;
}

const CARD = {
  type: "candidate_list",
  title: "长越科技｜机械高级工程师（岗位 137）候选名单",
  context: { type: "job", id: 137 },
  summary: { total: 10, active: 8, stopped: 2, bonder_count: 0 },
  groups: [
    { key: "active", label: "其余未停止候选", priority: false, candidates: [{ id: 1, name: "甲" }] },
    { key: "stopped", label: "已停止推进", priority: false, candidates: [{ id: 2, name: "乙" }] },
  ],
};

describe("asa_pool_filter", () => {
  it("execute：参数透传到 POST body，返回 {answer, card}", async () => {
    let url;
    let body;
    const fetchMock = mock.method(globalThis, "fetch", async (u, init) => {
      url = String(u);
      body = JSON.parse(String(init.body));
      return { ok: true, status: 200, json: async () => ({ ok: true, answer: "名单如下", card: CARD }) };
    });
    try {
      const tool = registerTools().get("asa_pool_filter");
      const result = await tool.execute({ job_id: 137, filter_mode: "grade_filter", bonder: true }, { signal: undefined });
      assert.match(url, /\/api\/v1\/jobs\/137\/candidate-list\/refresh$/);
      assert.equal(body.filter_mode, "grade_filter");
      assert.equal(body.bonder, true);
      assert.equal(typeof body.request_id, "string"); // postJson 自动附带幂等 request_id
      assert.equal(result.answer, "名单如下");
      assert.deepEqual(result.card, CARD);
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("execute：缺省参数时 bonder=false、filter_mode=''（宽松口径）", async () => {
    let body;
    const fetchMock = mock.method(globalThis, "fetch", async (_u, init) => {
      body = JSON.parse(String(init.body));
      return { ok: true, status: 200, json: async () => ({ ok: true, answer: "", card: CARD }) };
    });
    try {
      const tool = registerTools().get("asa_pool_filter");
      await tool.execute({ job_id: 154 }, { signal: undefined });
      assert.equal(body.bonder, false);
      assert.equal(body.filter_mode, "");
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("execute：非法 job_id 直接抛错，不发请求", async () => {
    const fetchMock = mock.method(globalThis, "fetch", async () => {
      throw new Error("不应发请求");
    });
    try {
      const tool = registerTools().get("asa_pool_filter");
      // 非整数由 dsh-tools 参数 schema 校验拦截（ToolArgsError），负数由工具自身护栏拦截。
      await assert.rejects(tool.execute({ job_id: "abc" }, { signal: undefined }), /must be an integer/);
      await assert.rejects(tool.execute({ job_id: -3 }, { signal: undefined }), /job_id 为正整数/);
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("execute：Core 非 2xx（如 404 岗位不存在）抛出带状态的错误", async () => {
    const fetchMock = mock.method(globalThis, "fetch", async () => ({
      ok: false, status: 404, json: async () => ({ detail: "job not found" }),
    }));
    try {
      const tool = registerTools().get("asa_pool_filter");
      await assert.rejects(tool.execute({ job_id: 999999 }, { signal: undefined }), /HTTP 404/);
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("presentationMeta：card 投影为 meta.action_card + object_refs 岗位引用", () => {
    const tool = registerTools().get("asa_pool_filter");
    assert.equal(typeof tool.output.presentationMeta, "function");
    const meta = tool.output.presentationMeta({}, { ok: true, answer: "x", card: CARD });
    assert.deepEqual(meta.action_card, CARD);
    assert.deepEqual(meta.object_refs, [
      { type: "job", id: 137, label: "长越科技｜机械高级工程师（岗位 137）候选名单" },
    ]);
  });

  it("presentationMeta：无 card 时 action_card=null、object_refs=[]，绝不返回 undefined", () => {
    const tool = registerTools().get("asa_pool_filter");
    // dsh-tools 对 presentationMeta 返回 undefined 会抛「non-lossless JSON」，必须总是对象。
    assert.deepEqual(tool.output.presentationMeta({}, { answer: "x" }), { action_card: null, object_refs: [] });
    assert.deepEqual(tool.output.presentationMeta({}, null), { action_card: null, object_refs: [] });
    // card 无 job context 时只出卡不出引用。
    const noCtx = tool.output.presentationMeta({}, { card: { type: "candidate_list", title: "t" } });
    assert.deepEqual(noCtx.object_refs, []);
  });
});
