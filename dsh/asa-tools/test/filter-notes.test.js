// asa_job_filter_notes / asa_job_filter_note_preflight 工具单测（dogfood R2-3：
// 岗位级筛选口径便签——跨会话"以后筛选按 X 口径"的持久化通道；写只暴露预检申请）。
import assert from "node:assert/strict";
import { describe, it, mock } from "node:test";

import { apply } from "../lib/index.js";

function registerTools() {
  const tools = new Map();
  const ctx = { tools: { register: (definition) => tools.set(definition.name, definition) } };
  apply(ctx);
  return tools;
}

const NOTE_PAYLOAD = {
  ok: true,
  job_id: 137,
  job: { id: 137, title: "机械高级工程师", client: "长越科技" },
  note: { note: "六自由度运动台作为大加分项", updated_by: "consultant", updated_at: "2026-08-19 23:50:00" },
};

describe("asa_job_filter_notes（只读口径便签）", () => {
  it("execute：GET 对应岗位便签，原样返回", async () => {
    let url;
    let method;
    const fetchMock = mock.method(globalThis, "fetch", async (u, init) => {
      url = String(u);
      method = init && init.method ? init.method : "GET";
      return { ok: true, status: 200, json: async () => NOTE_PAYLOAD };
    });
    try {
      const tool = registerTools().get("asa_job_filter_notes");
      const result = await tool.execute({ job_id: 137 }, { signal: undefined });
      assert.match(url, /\/api\/v1\/jobs\/137\/filter-notes$/);
      assert.equal(method, "GET");
      assert.equal(result.note.note, "六自由度运动台作为大加分项");
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("execute：非法 job_id 直接报错（不发请求）", async () => {
    const fetchMock = mock.method(globalThis, "fetch", async () => ({ ok: true, status: 200, json: async () => ({}) }));
    try {
      const tool = registerTools().get("asa_job_filter_notes");
      await assert.rejects(() => tool.execute({ job_id: 0 }, { signal: undefined }), /正整数/);
    } finally {
      fetchMock.mock.restore();
    }
  });
});

describe("asa_job_filter_note_preflight（口径便签写申请）", () => {
  it("execute：POST preflight 带 note，confirm_request 投影 kind=filter_note", async () => {
    let url;
    let body;
    const preflight = {
      ok: true,
      token: "tok-fn-1",
      expires_at: "2099-01-01T00:00:00",
      action: "job_filter_note",
      job: { id: 137, title: "机械高级工程师", client: "长越科技" },
      note: "六自由度运动台作为大加分项",
      previous_note: "",
      impact: "确认后保存为该岗位的筛选口径便签。",
    };
    const fetchMock = mock.method(globalThis, "fetch", async (u, init) => {
      url = String(u);
      body = JSON.parse(String(init.body));
      return { ok: true, status: 200, json: async () => preflight };
    });
    try {
      const tool = registerTools().get("asa_job_filter_note_preflight");
      const result = await tool.execute({ job_id: 137, note: "六自由度运动台作为大加分项" }, { signal: undefined });
      assert.match(url, /\/api\/v1\/jobs\/137\/filter-notes\/preflight$/);
      assert.equal(body.note, "六自由度运动台作为大加分项");
      assert.equal(typeof body.request_id, "string");
      assert.equal(result.token, "tok-fn-1");
      // presentationMeta：confirm_request 投影（常驻服务器据此透传 SSE → 前端确认卡）
      const meta = tool.output.presentationMeta({}, result);
      assert.equal(meta.confirm_request.kind, "filter_note");
      assert.equal(meta.confirm_request.preflight_token, "tok-fn-1");
      assert.equal(meta.confirm_request.job.id, 137);
      assert.equal(meta.confirm_request.note, "六自由度运动台作为大加分项");
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("execute：空 note / 非法 job_id 直接报错（不发请求）", async () => {
    const fetchMock = mock.method(globalThis, "fetch", async () => ({ ok: true, status: 200, json: async () => ({}) }));
    try {
      const tool = registerTools().get("asa_job_filter_note_preflight");
      await assert.rejects(() => tool.execute({ job_id: 137, note: "  " }, { signal: undefined }), /note 非空/);
      await assert.rejects(() => tool.execute({ job_id: -1, note: "口径" }, { signal: undefined }), /正整数/);
      assert.equal(fetchMock.mock.callCount(), 0);
    } finally {
      fetchMock.mock.restore();
    }
  });
});
