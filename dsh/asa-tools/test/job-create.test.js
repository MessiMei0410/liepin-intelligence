// asa_job_create_preflight 工具单测（岗位建档走确认链：模型只发起预检申请，
// confirm_request 投影 kind=job_create → 常驻服务器透传 SSE → 前端确认卡）。
import assert from "node:assert/strict";
import { describe, it, mock } from "node:test";

import { apply } from "../lib/index.js";

function registerTools() {
  const tools = new Map();
  const ctx = { tools: { register: (definition) => tools.set(definition.name, definition) } };
  apply(ctx);
  return tools;
}

describe("asa_job_create_preflight（岗位建档写申请）", () => {
  it("execute：POST /api/v1/jobs/preflight 带全部字段，confirm_request 投影 kind=job_create", async () => {
    let url;
    let body;
    const preflight = {
      ok: true,
      token: "tok-jc-1",
      expires_at: "2099-01-01T00:00:00",
      action: "job_create",
      job: {
        client: "杭州士兰微电子有限公司", client_id: 2, client_is_new: false, client_match: "fuzzy",
        title: "市场总监", direction: "汽车市场", base: "杭州", priority: "", jd_text: "",
      },
      warnings: ["客户名「士兰微」按既有客户「杭州士兰微电子有限公司」匹配建档", "未提供 JD 文本：建档后岗位职责/要求为空，可后续补充"],
      impact: "确认后将在既有客户「杭州士兰微电子有限公司」下建档岗位「市场总监」（初始状态：待启动）。",
    };
    const fetchMock = mock.method(globalThis, "fetch", async (u, init) => {
      url = String(u);
      body = JSON.parse(String(init.body));
      return { ok: true, status: 200, json: async () => preflight };
    });
    try {
      const tool = registerTools().get("asa_job_create_preflight");
      const result = await tool.execute(
        { client_name: "士兰微", title: "市场总监", direction: "汽车市场", base: "杭州" },
        { signal: undefined },
      );
      assert.match(url, /\/api\/v1\/jobs\/preflight$/);
      assert.equal(body.client_name, "士兰微");
      assert.equal(body.title, "市场总监");
      assert.equal(body.direction, "汽车市场");
      assert.equal(body.base, "杭州");
      assert.equal(body.jd_text, "");
      assert.equal(typeof body.request_id, "string");
      assert.equal(result.token, "tok-jc-1");
      // presentationMeta：confirm_request 投影（带全部字段回显 + 警告）
      const meta = tool.output.presentationMeta({}, result);
      assert.equal(meta.confirm_request.kind, "job_create");
      assert.equal(meta.confirm_request.preflight_token, "tok-jc-1");
      assert.equal(meta.confirm_request.job.client, "杭州士兰微电子有限公司");
      assert.equal(meta.confirm_request.job.title, "市场总监");
      assert.equal(meta.confirm_request.job.direction, "汽车市场");
      assert.equal(meta.confirm_request.job.base, "杭州");
      assert.equal(meta.confirm_request.warnings.length, 2);
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("execute：空 client_name / 空 title 直接报错（不发请求）", async () => {
    const fetchMock = mock.method(globalThis, "fetch", async () => ({ ok: true, status: 200, json: async () => ({}) }));
    try {
      const tool = registerTools().get("asa_job_create_preflight");
      await assert.rejects(() => tool.execute({ client_name: "  ", title: "市场总监" }, { signal: undefined }), /client_name 非空/);
      await assert.rejects(() => tool.execute({ client_name: "士兰微", title: "" }, { signal: undefined }), /title 非空/);
      assert.equal(fetchMock.mock.callCount(), 0);
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("execute：Core 409（重复岗位）原样抛错", async () => {
    const fetchMock = mock.method(globalThis, "fetch", async () => ({
      ok: false,
      status: 409,
      json: async () => ({ detail: "客户「长越科技」下已存在同名岗位（#137）" }),
    }));
    try {
      const tool = registerTools().get("asa_job_create_preflight");
      await assert.rejects(
        () => tool.execute({ client_name: "长越科技", title: "机械高级工程师" }, { signal: undefined }),
        /已存在同名岗位/,
      );
    } finally {
      fetchMock.mock.restore();
    }
  });
});
