// asa_candidate_profile 工具单测（node --test，本机跑；CI 暂无 dsh JS 测试门禁）。
// 验证：详情裁剪形态（只暴露档案字段，不带 events 全量等重字段）、
// full_text 8000 字截断护栏、object_refs 投影。
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { apply } from "../lib/index.js";

function registerTools() {
  const tools = new Map();
  const ctx = { tools: { register: (definition) => tools.set(definition.name, definition) } };
  apply(ctx);
  return tools;
}

const DETAIL = {
  ok: true,
  candidate: {
    id: 969, person_id: 960, name: "武先生",
    current_company: "晶盛机电", current_title: "机械工程师",
    city: "杭州", education: "本科", experience: "15年",
    client: "长越科技", job: "机械高级工程师", clean_stage: "触达待核验",
    is_stopped: false, stop_reason_label: "",
    resume: {
      summary: "摘要", work_text: "工作经历", project_text: "项目",
      education_text: "教育", full_text: "x".repeat(9000), raw: { huge: true },
    },
    events: [{ id: 1 }, { id: 2 }, { id: 3 }, { id: 4 }, { id: 5 }, { id: 6 }, { id: 7 }],
  },
};

describe("asa_candidate_profile", () => {
  it("execute：返回档案裁剪形态 + full_text 截断 + 最近 5 条事件", async () => {
    const tool = registerTools().get("asa_candidate_profile");
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async () => ({ ok: true, json: async () => DETAIL });
    try {
      const result = await tool.execute({ candidate_id: 969 }, { signal: undefined });
      const c = result.candidate;
      assert.equal(c.id, 969);
      assert.equal(c.person_id, 960);
      assert.equal(c.resume.summary, "摘要");
      assert.equal(c.resume.raw, undefined); // raw 重字段不下发
      assert.ok(c.resume.full_text.includes("已截断"));
      assert.ok(c.resume.full_text.length < 9000);
      assert.equal(c.recent_events.length, 5);
      assert.deepEqual(c.recent_events.map((e) => e.id), [3, 4, 5, 6, 7]);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it("execute：resume 非对象/缺失时安全兜底", async () => {
    const tool = registerTools().get("asa_candidate_profile");
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async () => ({ ok: true, json: async () => ({ ok: true, candidate: { id: 1, name: "甲", resume: null } }) });
    try {
      const result = await tool.execute({ candidate_id: 1 }, { signal: undefined });
      assert.equal(result.candidate.resume.full_text, "");
      assert.deepEqual(result.candidate.recent_events, []);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it("presentationMeta：候选人引用投影，无 candidate 时为空", () => {
    const tool = registerTools().get("asa_candidate_profile");
    assert.deepEqual(tool.output.presentationMeta({}, DETAIL), {
      object_refs: [{ type: "candidate", id: 969, label: "武先生", subtitle: "晶盛机电" }],
    });
    assert.deepEqual(tool.output.presentationMeta({}, { ok: true }), { object_refs: [] });
  });
});
