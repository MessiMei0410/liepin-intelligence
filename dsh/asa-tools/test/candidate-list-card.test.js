// asa_candidate_list_card 工具单测（node --test，本机跑；CI 暂无 dsh JS 测试门禁）。
// 验证：参数校验（candidate_ids 非空/正整数、title 非空、job_id 正整数）、参数透传
// （candidate_ids/title/groups/job_id→context → POST body）、{answer, card} 返回形态、
// presentationMeta 把 card 投影为 meta.action_card + object_refs 岗位引用（同
// asa_pool_filter 链路，常驻服务器据此透传 SSE card → 前端名单弹窗）。
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
  title: "长越机械｜精读通过名单",
  context: { type: "job", id: 137 },
  summary: { total: 2, active: 2, stopped: 0, requested: 2, skipped: [] },
  groups: [
    { key: "subset", label: "长越机械｜精读通过名单", priority: false, candidates: [{ id: 522, name: "甲" }, { id: 519, name: "乙" }] },
  ],
  subset: true,
};

describe("asa_candidate_list_card", () => {
  it("execute：参数透传到 POST body（含 groups 与 job_id→context），返回 {answer, card}", async () => {
    let url;
    let body;
    const fetchMock = mock.method(globalThis, "fetch", async (u, init) => {
      url = String(u);
      body = JSON.parse(String(init.body));
      return { ok: true, status: 200, json: async () => ({ ok: true, answer: "名单如下", card: CARD }) };
    });
    try {
      const tool = registerTools().get("asa_candidate_list_card");
      const result = await tool.execute({
        candidate_ids: [522, 519],
        title: "长越机械｜精读通过名单",
        groups: [{ key: "passed", label: "✅ 通过", candidate_ids: [522], priority: true }],
        job_id: 137,
      }, { signal: undefined });
      assert.match(url, /\/api\/v1\/candidates\/list-card$/);
      assert.deepEqual(body.candidate_ids, [522, 519]);
      assert.equal(body.title, "长越机械｜精读通过名单");
      assert.deepEqual(body.groups, [{ key: "passed", label: "✅ 通过", candidate_ids: [522], priority: true }]);
      assert.deepEqual(body.context, { type: "job", id: 137 });
      assert.equal(typeof body.request_id, "string"); // postJson 自动附带幂等 request_id
      assert.equal(result.answer, "名单如下");
      assert.deepEqual(result.card, CARD);
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("execute：缺省参数时不传 groups/context", async () => {
    let body;
    const fetchMock = mock.method(globalThis, "fetch", async (_u, init) => {
      body = JSON.parse(String(init.body));
      return { ok: true, status: 200, json: async () => ({ ok: true, answer: "", card: CARD }) };
    });
    try {
      const tool = registerTools().get("asa_candidate_list_card");
      await tool.execute({ candidate_ids: [522], title: "子集名单" }, { signal: undefined });
      assert.equal("groups" in body, false);
      assert.equal("context" in body, false);
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("execute：非法参数直接抛错，不发请求", async () => {
    const fetchMock = mock.method(globalThis, "fetch", async () => {
      throw new Error("不应发请求");
    });
    try {
      const tool = registerTools().get("asa_candidate_list_card");
      await assert.rejects(tool.execute({ candidate_ids: [], title: "t" }, { signal: undefined }), /candidate_ids 为非空数组/);
      // 缺 candidate_ids 由 dsh-tools 参数 schema 校验拦截（ToolArgsError），同 asa_pool_filter。
      await assert.rejects(tool.execute({ title: "t" }, { signal: undefined }), /missing required property "candidate_ids"/);
      await assert.rejects(tool.execute({ candidate_ids: [522, -1], title: "t" }, { signal: undefined }), /正整数/);
      await assert.rejects(tool.execute({ candidate_ids: ["abc"], title: "t" }, { signal: undefined }), /正整数/);
      await assert.rejects(tool.execute({ candidate_ids: [522], title: "  " }, { signal: undefined }), /title 非空/);
      await assert.rejects(tool.execute({ candidate_ids: [522], title: "t", job_id: -3 }, { signal: undefined }), /job_id 为正整数/);
      await assert.rejects(
        tool.execute({ candidate_ids: [522], title: "t", groups: [{ key: "g", label: "G", candidate_ids: [0] }] }, { signal: undefined }),
        /groups\[0\]\.candidate_ids 必须全部是正整数/,
      );
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("execute：Core 非 2xx（如 409 空数组）抛出带状态的错误", async () => {
    const fetchMock = mock.method(globalThis, "fetch", async () => ({
      ok: false, status: 409, json: async () => ({ detail: "candidate_ids 不能为空" }),
    }));
    try {
      const tool = registerTools().get("asa_candidate_list_card");
      await assert.rejects(tool.execute({ candidate_ids: [522], title: "t" }, { signal: undefined }), /HTTP 409/);
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("presentationMeta：card 投影为 meta.action_card + object_refs 岗位引用", () => {
    const tool = registerTools().get("asa_candidate_list_card");
    assert.equal(typeof tool.output.presentationMeta, "function");
    const meta = tool.output.presentationMeta({}, { ok: true, answer: "x", card: CARD });
    assert.deepEqual(meta.action_card, CARD);
    assert.deepEqual(meta.object_refs, [
      { type: "job", id: 137, label: "长越机械｜精读通过名单" },
    ]);
  });

  it("presentationMeta：无 card/无 context 时退化安全，绝不返回 undefined", () => {
    const tool = registerTools().get("asa_candidate_list_card");
    // dsh-tools 对 presentationMeta 返回 undefined 会抛「non-lossless JSON」，必须总是对象。
    assert.deepEqual(tool.output.presentationMeta({}, { answer: "x" }), { action_card: null, object_refs: [] });
    assert.deepEqual(tool.output.presentationMeta({}, null), { action_card: null, object_refs: [] });
    // 跨岗位子集（无 context）：只出卡不出引用。
    const noCtx = tool.output.presentationMeta({}, { card: { ...CARD, context: null } });
    assert.deepEqual(noCtx.object_refs, []);
    assert.equal(noCtx.action_card.type, "candidate_list");
  });
});
