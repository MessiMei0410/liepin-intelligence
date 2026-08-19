// 本地日期锚点单测（dogfood R2-2：8-19 23:39 本地说"明天下午两点一面"被记成 8-21）。
// 锚点文本必须含正确的本机本地日期/星期——模型推算"明天/下周"的唯一基准。
import assert from "node:assert/strict";
import { describe, it } from "node:test";

process.env.ASA_DSH_TOKEN = "date-anchor-test-token";
const { apply, localDateAnchorText } = await import("../lib/index.js");

describe("localDateAnchorText", () => {
  it("锚点文本含正确的本地日期、时分、星期与时区", () => {
    // 2026-08-19 23:39 Asia/Shanghai（= 15:39 UTC）：本地日期必须是 08-19 星期三。
    const now = new Date("2026-08-19T15:39:00Z");
    const text = localDateAnchorText(now, "Asia/Shanghai");
    assert.ok(text.includes("2026-08-19 23:39"), `锚点含本地日期时分：${text}`);
    assert.ok(text.includes("星期三"), `锚点含星期：${text}`);
    assert.ok(text.includes("Asia/Shanghai"), `锚点含时区：${text}`);
    assert.ok(text.includes("明天"), "锚点带相对日期推算约束");
  });

  it("UTC 深夜跨日场景以本地日期为准（本地 8-20 凌晨 ≠ UTC 8-19）", () => {
    // UTC 2026-08-19 23:30 = 本地 2026-08-20 07:30：锚点必须是 8-20 星期四。
    const text = localDateAnchorText(new Date("2026-08-19T23:30:00Z"), "Asia/Shanghai");
    assert.ok(text.includes("2026-08-20 07:30"), `跨日锚本地日期：${text}`);
    assert.ok(text.includes("星期四"), `跨日锚星期：${text}`);
  });

  it("默认取进程本地时区与当前时刻", () => {
    const text = localDateAnchorText();
    assert.match(text, /^\[当前本地时间：\d{4}-\d{2}-\d{2} \d{2}:\d{2}（星期[一二三四五六日]，.+\）/);
  });
});

describe("每轮注入日期锚点", () => {
  it("用户消息带日期锚点前缀（无焦点时也不缺）", async () => {
    const sent = [];
    const ctx = {
      get(service) {
        if (service === "agentDefaultModel") return { currentSelection: () => ({ provider: "p", model: "m" }) };
        if (service === "agents") {
          return {
            async create() {
              return {
                agent: {
                  session: { seq: 0 },
                  ctx: { on: () => () => {} },
                  followup(message) { sent.push(message.content.map((block) => block.text).join("")); },
                  cancel() {},
                  async whenIdle() {},
                },
                dispose: async () => {},
              };
            },
          };
        }
        throw new Error(`unexpected ctx.get: ${service}`);
      },
    };
    process.env.ASA_DSH_RESIDENT_PORT = String(9300 + Math.floor(Math.random() * 90));
    process.env.ASA_CORE_URL = "http://127.0.0.1:1"; // 回填必然失败（无 Core），不影响本断言
    const server = apply(ctx);
    await new Promise((resolve) => setTimeout(resolve, 200));
    const port = Number(process.env.ASA_DSH_RESIDENT_PORT);
    try {
      await fetch(`http://127.0.0.1:${port}/turn`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer date-anchor-test-token" },
        body: JSON.stringify({ message: "明天下午两点一面帮我记下来", session_id: "asa-date-1" }),
      }).then((res) => res.text());
      assert.equal(sent.length, 1);
      assert.match(sent[0], /^\[当前本地时间：\d{4}-\d{2}-\d{2} \d{2}:\d{2}（星期[一二三四五六日]，/);
      assert.ok(sent[0].includes("一律以该本地日期为准推算"), "锚点带推算约束");
      assert.ok(sent[0].endsWith("明天下午两点一面帮我记下来"), "用户原文保留在锚点之后");
    } finally {
      server.close();
    }
  });
});
