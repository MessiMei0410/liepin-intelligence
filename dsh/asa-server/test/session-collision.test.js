// 会话碰撞自愈单测（node --test，本机跑；CI 暂无 dsh JS 测试门禁）。
// 2026-08-19 实证：部署重启恰逢在跑轮次 → 陈旧持久化日志与新 agent 事件流不匹配
// → agents.create 抛 id collision → 该会话每轮毫秒级失败。自愈：归档日志并重试一次。
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, existsSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

// TOKEN 在模块加载时读取，先注入再动态 import（不读本机真实 token 文件）。
process.env.ASA_DSH_TOKEN = "collision-test-token";
const { apply, archiveStaleSessionLog, encodeSegment, projectKey } = await import("../lib/index.js");

const SESSION_ID = "asa-collision-test-0001";

function fakeAgent() {
  return {
    session: { seq: 0 },
    ctx: { on: () => () => {} },
    followup() {},
    cancel() {},
    async whenIdle() {},
  };
}

function startServer(ctx, port) {
  process.env.ASA_DSH_RESIDENT_PORT = String(port);
  process.env.ASA_DSH_TOKEN = "";
  return apply(ctx);
}

describe("会话碰撞自愈", () => {
  it("archiveStaleSessionLog：日志挪到 .bak-*，不存在则空操作", () => {
    const root = mkdtempSync(join(tmpdir(), "asa-dsh-sessions-"));
    process.env.ASA_DSH_SESSIONS_ROOT = root;
    const dir = join(root, projectKey(process.cwd()), encodeSegment(SESSION_ID));
    mkdirSync(dir, { recursive: true });
    const log = join(dir, "session.jsonl.zstd");
    writeFileSync(log, "stale");
    archiveStaleSessionLog(SESSION_ID);
    assert.equal(existsSync(log), false);
    assert.ok(readdirSync(dir).some((name) => name.startsWith("session.jsonl.zstd.bak-")));
    // 第二次调用无文件不抛错
    archiveStaleSessionLog(SESSION_ID);
  });

  it("agents.create 抛 id collision 时归档并重试一次，轮次照常完成", async () => {
    const root = mkdtempSync(join(tmpdir(), "asa-dsh-sessions-"));
    process.env.ASA_DSH_SESSIONS_ROOT = root;
    const dir = join(root, projectKey(process.cwd()), encodeSegment(SESSION_ID));
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "session.jsonl.zstd"), "stale");

    let createCalls = 0;
    const ctx = {
      get(service) {
        if (service === "agentDefaultModel") return { currentSelection: () => ({ provider: "p", model: "m" }) };
        if (service === "agents") {
          return {
            async create() {
              createCalls += 1;
              if (createCalls === 1) {
                throw new Error(`session "${SESSION_ID}" already has a persisted log on disk that does not match this live session (id collision)`);
              }
              return { agent: fakeAgent(), dispose: async () => {} };
            },
          };
        }
        throw new Error(`unexpected ctx.get: ${service}`);
      },
    };
    const port = 8900 + Math.floor(Math.random() * 90);
    const server = startServer(ctx, port);
    await new Promise((resolve) => setTimeout(resolve, 200));
    try {
      const res = await fetch(`http://127.0.0.1:${port}/turn`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer collision-test-token" },
        body: JSON.stringify({ message: "hi", session_id: SESSION_ID }),
      });
      const text = await res.text();
      assert.equal(createCalls, 2); // 碰撞后重试了一次
      assert.ok(text.includes("event: done")); // 轮次走完了 SSE 全流程
      assert.ok(readdirSync(dir).some((name) => name.startsWith("session.jsonl.zstd.bak-")));
    } finally {
      server.close();
    }
  });
});
