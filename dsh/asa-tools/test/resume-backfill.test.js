// asa_resume_backfill 工具面单测（node --test）。
// 验证：
// - 参数护栏：candidate_id / resume_id 至少其一；
// - 只调预检端点（POST /api/v1/candidates/resume-backfill/preflight，不带写语义）；
// - presentationMeta 投影 confirm_request（kind=resume_backfill，含 diff）；
// - unchanged（无变化）结果不投确认卡（confirm_request=null）。
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

const PREFLIGHT = {
  ok: true,
  token: "tok-rb-1",
  expires_at: "2026-08-19T12:00:00",
  action: "resume_backfill",
  candidate: { id: 901, name: "杜明", stage: "S1 新增寻访/待复核", client: "华虹客户", job: "设备工程师" },
  resume: { resume_id: "res-du-1", source_url: "https://h.liepin.com/resume/showresumedetail/?res_id_encode=res-du-1", captured_at: "2026-08-19T10:00:00", full_text_chars: 1600 },
  diff: [
    { field: "full_text", label: "简历全文", change: "updated", before_chars: 900, after_chars: 1600, before_excerpt: "旧…", after_excerpt: "新…" },
    { field: "work_text", label: "工作经历", change: "added", before_chars: 0, after_chars: 300, before_excerpt: "", after_excerpt: "华虹半导体…" },
  ],
  impact: "简历档案将按当前页快照更新。",
};

describe("asa_resume_backfill", () => {
  it("candidate_id 与 resume_id 都缺省时直接报错（不发请求）", async () => {
    const fetchMock = stubFetchJson(PREFLIGHT);
    try {
      const tool = registerTools().get("asa_resume_backfill");
      await assert.rejects(() => tool.execute({}, { signal: undefined }), /至少提供其一/);
      assert.equal(fetchMock.mock.calls.length, 0);
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("调预检端点并把 confirm_request（含 diff）投影到 presentationMeta", async () => {
    const fetchMock = stubFetchJson(PREFLIGHT);
    try {
      const tool = registerTools().get("asa_resume_backfill");
      const result = await tool.execute({ candidate_id: 901, resume_id: "res-du-1" }, { signal: undefined });
      const [url, init] = fetchMock.mock.calls[0].arguments;
      assert.ok(String(url).endsWith("/api/v1/candidates/resume-backfill/preflight"));
      assert.equal(init.method, "POST");
      const body = JSON.parse(init.body);
      assert.equal(body.candidate_id, 901);
      assert.equal(body.resume_id, "res-du-1");
      assert.ok(body.request_id, "写信封带 request_id");
      assert.ok(!String(init.headers["User-Agent"]).startsWith("ASAApp/"), "工具通道 UA 不得为 ASAApp/ 前缀");
      const meta = tool.output.presentationMeta({}, result);
      const confirm = meta.confirm_request;
      assert.equal(confirm.kind, "resume_backfill");
      assert.equal(confirm.preflight_token, "tok-rb-1");
      assert.equal(confirm.expires_at, "2026-08-19T12:00:00");
      assert.equal(confirm.candidate.name, "杜明");
      assert.equal(confirm.resume.resume_id, "res-du-1");
      assert.equal(confirm.diff.length, 2);
      assert.equal(confirm.diff[0].change, "updated");
      assert.ok(confirm.impact);
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("仅 resume_id 也可发起（反查本地人选）", async () => {
    const fetchMock = stubFetchJson(PREFLIGHT);
    try {
      const tool = registerTools().get("asa_resume_backfill");
      await tool.execute({ resume_id: "res-du-1" }, { signal: undefined });
      const body = JSON.parse(fetchMock.mock.calls[0].arguments[1].body);
      assert.deepEqual({ candidate_id: body.candidate_id, resume_id: body.resume_id }, { candidate_id: undefined, resume_id: "res-du-1" });
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("unchanged（档案已是最新）不投确认卡", async () => {
    const fetchMock = stubFetchJson({ ok: true, unchanged: true, action: "resume_backfill", candidate: PREFLIGHT.candidate, resume: PREFLIGHT.resume, diff: [], message: "页面简历与本地档案一致，无需回填。" });
    try {
      const tool = registerTools().get("asa_resume_backfill");
      const result = await tool.execute({ candidate_id: 901 }, { signal: undefined });
      const meta = tool.output.presentationMeta({}, result);
      assert.equal(meta.confirm_request, null);
    } finally {
      fetchMock.mock.restore();
    }
  });
});
