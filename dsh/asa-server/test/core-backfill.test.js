// 服务端回填 Core 单测（dogfood R2-1：流式中途刷新/关页/断网时 done 到不了前端，
// 前端回填不发生，新任务首轮整会话丢失）。asa-server 轮末自己回填 Core：
// - completed 轮：全量字段（答案/卡片/确认请求注入 client_request_id）；
// - 非 completed 轮（aborted/超时）：最小回填 + turn_error；
// - 幂等键 = 前端经 /turn 带上来的 request_id（Core 原子去重，与前端回填先到先赢）；
// - 请求体没带 request_id（旧前端）时不回填，避免双份。
import assert from "node:assert/strict";
import http from "node:http";
import { describe, it } from "node:test";

process.env.ASA_DSH_TOKEN = "backfill-test-token";
const { apply, buildBackfillBody, backfillTurnToCore } = await import("../lib/index.js");

describe("buildBackfillBody", () => {
  const base = {
    sessionId: "asa-s1", requestId: "agent_req-1", message: "问句",
    context: { type: "job", id: 137 },
  };

  it("completed 轮：全量字段 + confirm_request 注入 client_request_id", () => {
    const body = buildBackfillBody({
      ...base,
      done: {
        session_id: "asa-s1", ok: true, answer: "完整答案",
        action_card: { type: "candidate_list", title: "名单" },
        action_cards: [{ type: "candidate_list", title: "名单" }],
        subagents: [{ id: "run-1", status: "done" }],
        suggested_actions: [{ type: "open_job", id: 137 }],
        references: [{ type: "job", id: 137, label: "机械岗" }],
        confirm_request: { kind: "candidate_action", preflight_token: "tok" },
        understanding_card: { show: true },
        workflow_id: "workflow_1",
        workflow_progress: { workflow_id: "workflow_1", status: "running" },
      },
    });
    assert.equal(body.session_id, "asa-s1");
    assert.equal(body.request_id, "agent_req-1");
    assert.equal(body.message, "问句");
    assert.equal(body.answer, "完整答案");
    assert.equal(body.source, "dsh");
    assert.deepEqual(body.context, { type: "job", id: 137 });
    assert.equal(body.action_card.type, "candidate_list");
    assert.equal(body.confirm_request.client_request_id, "agent_req-1");
    assert.equal(body.confirm_request.preflight_token, "tok");
    assert.equal(body.workflow_id, "workflow_1");
    assert.equal(body.turn_error, undefined);
  });

  it("非 completed 轮（aborted/超时）：最小回填 + turn_error，不带卡片", () => {
    const body = buildBackfillBody({
      ...base,
      done: {
        session_id: "asa-s1", ok: false, answer: "部分答案",
        error: "turn aborted (client-disconnect)",
        action_card: { type: "candidate_list" },
        confirm_request: { kind: "candidate_action" },
      },
    });
    assert.equal(body.answer, "部分答案");
    assert.equal(body.turn_error, "turn aborted (client-disconnect)");
    assert.equal(body.action_card, undefined);
    assert.equal(body.confirm_request, undefined);
  });

  it("error 缺省时 turn_error 有兜底文案", () => {
    const body = buildBackfillBody({ ...base, done: { session_id: "asa-s1", ok: false, answer: "" } });
    assert.equal(body.turn_error, "turn did not complete");
  });
});

describe("backfillTurnToCore", () => {
  it("2xx 直返 true；非 2xx 重试后仍失败返回 false", async () => {
    const ok = await backfillTurnToCore(
      { session_id: "s", request_id: "r" },
      { fetchImpl: async () => ({ ok: true, status: 200 }), attempts: 2, baseDelayMs: 1 },
    );
    assert.equal(ok, true);

    let calls = 0;
    const failed = await backfillTurnToCore(
      { session_id: "s", request_id: "r" },
      {
        fetchImpl: async () => { calls += 1; return { ok: false, status: 500 }; },
        attempts: 3, baseDelayMs: 1,
      },
    );
    assert.equal(failed, false);
    assert.equal(calls, 3, "非 2xx 按次数重试");

    let seen = null;
    await backfillTurnToCore(
      { session_id: "s", request_id: "r" },
      {
        fetchImpl: async (url, init) => { seen = { url: String(url), body: JSON.parse(String(init.body)), ua: init.headers["User-Agent"] }; return { ok: true, status: 200 }; },
        attempts: 1, coreUrl: "http://127.0.0.1:8765",
      },
    );
    assert.match(seen.url, /\/api\/v1\/copilot\/sessions\/record-turn$/);
    assert.equal(seen.body.request_id, "r");
    assert.ok(!seen.ua.startsWith("ASAApp/"), "服务端回填 UA 不是 ASAApp 前缀（不进 UI 激活通道）");
  });
});

// ── /turn 集成：轮末服务端自动回填 Core ──────────────────────────────────

function fakeAgentWithEvents(sent) {
  const handlers = [];
  return {
    handle: {
      agent: null, // 回填在下方补齐
      dispose: async () => {},
    },
    agent: {
      session: { seq: 0 },
      ctx: { on: (name, fn) => { handlers.push([name, fn]); return () => {}; } },
      followup(message) { sent.push(message.content.map((block) => block.text).join("")); },
      cancel() {},
      async whenIdle() {
        const fire = handlers.find(([name]) => name === "session/event")?.[1];
        if (fire) {
          fire(null, { seq: 1, type: "assistant/message", data: { message: { content: [{ type: "text", text: "完整答案" }] } } });
          fire(null, { seq: 2, type: "turn/end", data: { reason: { kind: "completed" } } });
        }
      },
    },
  };
}

async function withFakeCore(run) {
  const received = [];
  const core = http.createServer((req, res) => {
    if (req.method === "POST" && req.url === "/api/v1/copilot/sessions/record-turn") {
      let raw = "";
      req.on("data", (chunk) => { raw += chunk; });
      req.on("end", () => {
        received.push(JSON.parse(raw));
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ ok: true, recorded: true }));
      });
      return;
    }
    res.writeHead(404);
    res.end();
  });
  await new Promise((resolve) => core.listen(0, "127.0.0.1", resolve));
  process.env.ASA_CORE_URL = `http://127.0.0.1:${core.address().port}`;
  try {
    await run(received);
  } finally {
    await new Promise((resolve) => core.close(resolve));
  }
}

function startResident(sent) {
  const ctx = {
    get(service) {
      if (service === "agentDefaultModel") return { currentSelection: () => ({ provider: "p", model: "m" }) };
      if (service === "agents") {
        return {
          async create() {
            const fake = fakeAgentWithEvents(sent);
            return { agent: fake.agent, dispose: async () => {} };
          },
        };
      }
      throw new Error(`unexpected ctx.get: ${service}`);
    },
  };
  process.env.ASA_DSH_RESIDENT_PORT = String(9400 + Math.floor(Math.random() * 90));
  return { server: apply(ctx), port: Number(process.env.ASA_DSH_RESIDENT_PORT) };
}

describe("asa-server 轮末服务端回填 Core", () => {
  it("completed 轮带 request_id：回填同幂等键 + 用户问句 + 答案", async () => {
    const sent = [];
    const { server, port } = startResident(sent);
    await new Promise((resolve) => setTimeout(resolve, 200));
    await withFakeCore(async (received) => {
      const text = await fetch(`http://127.0.0.1:${port}/turn`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer backfill-test-token" },
        body: JSON.stringify({ message: "把长越机械岗 A-强档全部核验一遍", session_id: "asa-bf-1", request_id: "agent_req-bf-1" }),
      }).then((res) => res.text());
      assert.ok(text.includes("event: done"), "前端仍收到 done");
      assert.equal(received.length, 1, "服务端回填恰好一次");
      assert.equal(received[0].request_id, "agent_req-bf-1");
      assert.equal(received[0].session_id, "asa-bf-1");
      assert.equal(received[0].message, "把长越机械岗 A-强档全部核验一遍");
      assert.equal(received[0].answer, "完整答案");
      assert.equal(received[0].turn_error, undefined, "completed 轮不带 turn_error");
    });
    server.close();
  });

  it("请求体不带 request_id（旧前端）：服务端不回填，避免与前端回填双份", async () => {
    const sent = [];
    const { server, port } = startResident(sent);
    await new Promise((resolve) => setTimeout(resolve, 200));
    await withFakeCore(async (received) => {
      await fetch(`http://127.0.0.1:${port}/turn`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer backfill-test-token" },
        body: JSON.stringify({ message: "你好", session_id: "asa-bf-2" }),
      }).then((res) => res.text());
      assert.equal(received.length, 0);
    });
    server.close();
  });

  it("中断轮（aborted）：回填用户问句 + 已流式部分答案 + turn_error", async () => {
    // 模拟刷新/断连场景：只流出部分 text-delta，assistant/message 从未落地，轮以 aborted 终局。
    const ctx = {
      get(service) {
        if (service === "agentDefaultModel") return { currentSelection: () => ({ provider: "p", model: "m" }) };
        if (service === "agents") {
          return {
            async create() {
              const handlers = [];
              return {
                agent: {
                  session: { seq: 0 },
                  ctx: { on: (name, fn) => { handlers.push([name, fn]); return () => {}; } },
                  followup() {},
                  cancel() {},
                  async whenIdle() {
                    const fire = handlers.find(([name]) => name === "session/event")?.[1];
                    if (fire) {
                      fire(null, { seq: 1, type: "assistant/chunk", data: { chunk: { type: "text-delta", text: "我先看下岗位管道数据……" } } });
                      fire(null, { seq: 2, type: "turn/end", data: { reason: { kind: "aborted", reason: { kind: "hook", reason: "client-disconnect" } } } });
                    }
                  },
                },
                dispose: async () => {},
              };
            },
          };
        }
        throw new Error(`unexpected ctx.get: ${service}`);
      },
    };
    process.env.ASA_DSH_RESIDENT_PORT = String(9500 + Math.floor(Math.random() * 90));
    const port = Number(process.env.ASA_DSH_RESIDENT_PORT);
    const server = apply(ctx);
    await new Promise((resolve) => setTimeout(resolve, 200));
    await withFakeCore(async (received) => {
      await fetch(`http://127.0.0.1:${port}/turn`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer backfill-test-token" },
        body: JSON.stringify({ message: "把长越机械岗 A-强档全部核验一遍", session_id: "asa-bf-3", request_id: "agent_req-bf-3" }),
      }).then((res) => res.text());
      assert.equal(received.length, 1);
      assert.equal(received[0].request_id, "agent_req-bf-3");
      assert.equal(received[0].message, "把长越机械岗 A-强档全部核验一遍");
      assert.equal(received[0].answer, "我先看下岗位管道数据……", "部分答案随中断轮回填");
      assert.equal(received[0].turn_error, "turn aborted (client-disconnect)");
    });
    server.close();
  });
});
