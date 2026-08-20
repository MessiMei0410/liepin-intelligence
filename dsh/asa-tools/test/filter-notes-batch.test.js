// asa_job_filter_notes_batch_preflight 工具单测（多岗位一张确认卡：
// 多岗位同类写申请一次发起，铸一个绑定整批 items 哈希的 token）。
import assert from "node:assert/strict";
import { describe, it, mock } from "node:test";

import { apply } from "../lib/index.js";

function registerTools() {
  const tools = new Map();
  const ctx = { tools: { register: (definition) => tools.set(definition.name, definition) } };
  apply(ctx);
  return tools;
}

const BATCH_PREFLIGHT = {
  ok: true,
  token: "tok-batch-1",
  expires_at: "2099-01-01T00:00:00",
  action: "job_filter_note_batch",
  items: [
    { job_id: 137, job: { id: 137, title: "机械高级工程师", client: "长越科技" }, note: "六自由度运动台作为大加分项", previous_note: "" },
    { job_id: 138, job: { id: 138, title: "软件高级工程师", client: "长越科技" }, note: "3-5 自由度为次优先", previous_note: "旧口径" },
  ],
  impact: "确认后一次性保存 2 个岗位的筛选口径便签。",
};

describe("asa_job_filter_notes_batch_preflight（批量口径便签写申请）", () => {
  it("execute：POST batch-preflight 带规范化 items，confirm_request 投影 kind=filter_note_batch", async () => {
    let url;
    let body;
    const fetchMock = mock.method(globalThis, "fetch", async (u, init) => {
      url = String(u);
      body = JSON.parse(String(init.body));
      return { ok: true, status: 200, json: async () => BATCH_PREFLIGHT };
    });
    try {
      const tool = registerTools().get("asa_job_filter_notes_batch_preflight");
      const result = await tool.execute({
        items: [
          { job_id: 137, note: "  六自由度运动台作为大加分项  " },
          { job_id: 138, note: "3-5 自由度为次优先" },
        ],
      }, { signal: undefined });
      assert.match(url, /\/api\/v1\/jobs\/filter-notes\/batch-preflight$/);
      assert.equal(typeof body.request_id, "string");
      assert.deepEqual(body.items, [
        { job_id: 137, note: "六自由度运动台作为大加分项" },
        { job_id: 138, note: "3-5 自由度为次优先" },
      ]);
      assert.equal(result.token, "tok-batch-1");
      // presentationMeta：confirm_request 投影带整批 items（前端渲染一张批量确认卡）
      const meta = tool.output.presentationMeta({}, result);
      assert.equal(meta.confirm_request.kind, "filter_note_batch");
      assert.equal(meta.confirm_request.preflight_token, "tok-batch-1");
      assert.equal(meta.confirm_request.action, "job_filter_note_batch");
      assert.equal(meta.confirm_request.items.length, 2);
      assert.equal(meta.confirm_request.items[0].job.client, "长越科技");
      assert.equal(meta.confirm_request.items[1].previous_note, "旧口径");
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("execute：空数组 / 超 50 项 / 非法 job_id / 空 note / 岗位重复 直接报错（不发请求）", async () => {
    const fetchMock = mock.method(globalThis, "fetch", async () => ({ ok: true, status: 200, json: async () => ({}) }));
    try {
      const tool = registerTools().get("asa_job_filter_notes_batch_preflight");
      await assert.rejects(() => tool.execute({ items: [] }, { signal: undefined }), /非空数组/);
      await assert.rejects(
        () => tool.execute({ items: Array.from({ length: 51 }, (_, i) => ({ job_id: i + 1, note: "口径" })) }, { signal: undefined }),
        /最多 50 项/,
      );
      await assert.rejects(
        () => tool.execute({ items: [{ job_id: -1, note: "口径" }] }, { signal: undefined }),
        /正整数/,
      );
      await assert.rejects(
        () => tool.execute({ items: [{ job_id: 137, note: "  " }] }, { signal: undefined }),
        /note 必须非空/,
      );
      await assert.rejects(
        () => tool.execute({ items: [{ job_id: 137, note: "口径 A" }, { job_id: 137, note: "口径 B" }] }, { signal: undefined }),
        /重复/,
      );
      assert.equal(fetchMock.mock.callCount(), 0);
    } finally {
      fetchMock.mock.restore();
    }
  });
});
