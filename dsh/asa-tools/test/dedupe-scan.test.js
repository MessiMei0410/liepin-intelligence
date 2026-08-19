// 去重扫描 + 合并预检工具面单测（node --test，本机跑；CI 暂无 dsh JS 测试门禁）。
// 验证：
// - asa_dedupe_scan 只读工具：GET /api/v1/candidates/dedupe-scan（job_id 可选），
//   扫描结果里的关系经 presentationMeta 投影 object_refs（轮末操作入口）；
// - asa_candidate_preflight(action=merge)：winner_id/loser_id 缺一不发请求直接报错；
//   POST body 带 loser_id；confirm_request 投影 merge（winner/loser/diff）供确认卡展示。
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

describe("asa_dedupe_scan（只读）", () => {
  it("默认全池扫描；带 job_id 时拼查询串", async () => {
    const fetchMock = stubFetchJson({ ok: true, group_count: 0, groups: [] });
    try {
      const tool = registerTools().get("asa_dedupe_scan");
      await tool.execute({}, { signal: undefined });
      assert.ok(String(fetchMock.mock.calls[0].arguments[0]).endsWith("/api/v1/candidates/dedupe-scan"));
      await tool.execute({ job_id: 137 }, { signal: undefined });
      assert.ok(String(fetchMock.mock.calls[1].arguments[0]).endsWith("/api/v1/candidates/dedupe-scan?job_id=137"));
      // 只读：只能是 GET（无 method/ body）。
      assert.equal(fetchMock.mock.calls[0].arguments[1].method, undefined);
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("扫描组成员投影 object_refs（候选人操作入口）", async () => {
    const scan = {
      ok: true,
      group_count: 1,
      groups: [{
        group_id: "dup_1",
        surname: "武",
        suggested_winner_id: 969,
        members: [
          { relation_id: 969, name: "武先生", current_company: "晶盛机电（半导体、光伏设备）" },
          { relation_id: 546, name: "武斌", current_company: "晶盛机电" },
        ],
      }],
    };
    const fetchMock = stubFetchJson(scan);
    try {
      const tool = registerTools().get("asa_dedupe_scan");
      const result = await tool.execute({}, { signal: undefined });
      const meta = tool.output.presentationMeta({}, result);
      assert.deepEqual(
        meta.object_refs.map((ref) => [ref.type, ref.id]),
        [["candidate", 969], ["candidate", 546]],
      );
      assert.equal(meta.object_refs[0].label, "武先生");
    } finally {
      fetchMock.mock.restore();
    }
  });
});

describe("asa_candidate_preflight(action=merge)", () => {
  it("缺 loser_id 不发请求直接报错", async () => {
    const fetchMock = stubFetchJson({});
    try {
      const tool = registerTools().get("asa_candidate_preflight");
      await assert.rejects(
        () => tool.execute({ candidate_id: 969, action: "merge" }, { signal: undefined }),
        /winner_id 与 loser_id/,
      );
      assert.equal(fetchMock.mock.calls.length, 0);
    } finally {
      fetchMock.mock.restore();
    }
  });

  it("POST 预检带 loser_id，confirm_request 投影 merge diff", async () => {
    const preflight = {
      ok: true,
      token: "tok-merge",
      expires_at: "2026-08-19T12:00:00",
      action: "merge",
      candidate: { id: 969, name: "武先生", stage: "S1 新增寻访/待复核" },
      winner: { id: 969, name: "武先生" },
      loser: { id: 546, name: "武斌" },
      diff: [
        { field: "name", label: "姓名", winner: "武先生", loser: "武斌", same: false },
        { field: "current_company", label: "当前公司", winner: "晶盛机电（半导体、光伏设备）", loser: "晶盛机电", same: false },
      ],
      loser_already_stopped: false,
      impact: "合并不物理删行：废弃方关系将停止推进（停止原因：重复人选）并备注指向保留方。",
    };
    const fetchMock = stubFetchJson(preflight);
    try {
      const tool = registerTools().get("asa_candidate_preflight");
      const result = await tool.execute({ candidate_id: 969, action: "merge", loser_id: 546 }, { signal: undefined });
      const [url, init] = fetchMock.mock.calls[0].arguments;
      assert.ok(String(url).endsWith("/api/v1/candidate-actions/preflight"));
      const body = JSON.parse(String(init.body));
      assert.equal(body.candidate_id, 969);
      assert.equal(body.action, "merge");
      assert.equal(body.loser_id, 546);
      const meta = tool.output.presentationMeta({}, result);
      assert.equal(meta.confirm_request.kind, "candidate_action");
      assert.equal(meta.confirm_request.action, "merge");
      assert.equal(meta.confirm_request.preflight_token, "tok-merge");
      assert.equal(meta.confirm_request.merge.winner.name, "武先生");
      assert.equal(meta.confirm_request.merge.loser.name, "武斌");
      assert.equal(meta.confirm_request.merge.diff.length, 2);
      // winner_id 显式传入时覆盖 candidate_id。
      await tool.execute({ candidate_id: 1, action: "merge", winner_id: 969, loser_id: 546 }, { signal: undefined });
      assert.equal(JSON.parse(String(fetchMock.mock.calls[1].arguments[1].body)).candidate_id, 969);
    } finally {
      fetchMock.mock.restore();
    }
  });
});
