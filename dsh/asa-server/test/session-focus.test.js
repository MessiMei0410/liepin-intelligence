// 会话业务焦点单测（dogfood P1-2：名单轮后追问「前 3 人精读」被答成另一个岗位 137→142）。
// 根因：/turn 不消费 payload.context，模型只能靠会话记忆猜指代。修复后：
// 显式业务上下文/名单卡上下文更新会话焦点，追问轮注入焦点锚点文本。
import assert from "node:assert/strict";
import { describe, it } from "node:test";

process.env.ASA_DSH_TOKEN = "focus-test-token";
const { apply, businessFocusOf, focusNoteText } = await import("../lib/index.js");

describe("businessFocusOf / focusNoteText", () => {
  it("只接受 job/candidate 业务上下文，page/global/非法 id 返回 null", () => {
    assert.deepEqual(businessFocusOf({ type: "job", id: 137 }), { type: "job", id: 137, label: "" });
    assert.deepEqual(businessFocusOf({ type: "candidate", id: 969, job: "机械高级工程师" }), { type: "candidate", id: 969, label: "机械高级工程师" });
    assert.equal(businessFocusOf({ type: "page", page: "agent" }), null);
    assert.equal(businessFocusOf({ type: "job", id: 0 }), null);
    assert.equal(businessFocusOf({ type: "job" }), null);
    assert.equal(businessFocusOf(null), null);
    assert.equal(businessFocusOf("job:137"), null);
  });

  it("focusNoteText 生成指代锚点文案", () => {
    assert.equal(focusNoteText(null), "");
    const note = focusNoteText({ type: "job", id: 137, label: "长越科技｜机械高级工程师" });
    assert.ok(note.includes("岗位 #137"));
    assert.ok(note.includes("长越科技｜机械高级工程师"));
  });
});

describe("会话焦点随轮次附着", () => {
  function fakeAgent(sent) {
    return {
      session: { seq: 0 },
      ctx: { on: () => () => {} },
      followup(message) {
        sent.push(message.content.map((block) => block.text).join(""));
      },
      cancel() {},
      async whenIdle() {},
    };
  }

  function startServer(ctx, port) {
    process.env.ASA_DSH_RESIDENT_PORT = String(port);
    return apply(ctx);
  }

  it("首轮带岗位上下文后，追问轮（page 上下文）自动锚定该岗位；显式切换则覆盖", async () => {
    const sent = [];
    const ctx = {
      get(service) {
        if (service === "agentDefaultModel") return { currentSelection: () => ({ provider: "p", model: "m" }) };
        if (service === "agents") return { async create() { return { agent: fakeAgent(sent), dispose: async () => {} }; } };
        throw new Error(`unexpected ctx.get: ${service}`);
      },
    };
    const port = 9100 + Math.floor(Math.random() * 90);
    const server = startServer(ctx, port);
    await new Promise((resolve) => setTimeout(resolve, 200));
    const post = (body) => fetch(`http://127.0.0.1:${port}/turn`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer focus-test-token" },
      body: JSON.stringify(body),
    }).then((res) => res.text());
    try {
      // 首轮：显式附着岗位 137
      await post({ message: "给长越机械岗筛一轮存量名单", session_id: "asa-focus-1", context: { type: "job", id: 137, label: "长越科技｜机械高级工程师" } });
      // 追问轮：前端只带 page 上下文，仍应锚定 137
      await post({ message: "前 3 人精读下", session_id: "asa-focus-1", context: { type: "page", page: "agent" } });
      // 显式切换到候选人 969：焦点跟随最近明确焦点
      await post({ message: "他最近什么动态", session_id: "asa-focus-1", context: { type: "candidate", id: 969 } });
      assert.equal(sent.length, 3);
      assert.ok(sent[0].includes("岗位 #137"), `首轮应带焦点锚点：${sent[0]}`);
      assert.ok(sent[1].includes("岗位 #137"), `追问轮应锚定最近焦点：${sent[1]}`);
      assert.ok(sent[1].includes("前 3 人精读下"), "用户原文保留");
      assert.ok(sent[2].includes("候选人 #969"), `显式切换应覆盖焦点：${sent[2]}`);
      assert.ok(!sent[2].includes("岗位 #137"), "旧焦点不再残留");
    } finally {
      server.close();
    }
  });

  it("全程 page 上下文时不注入焦点噪音", async () => {
    const sent = [];
    const ctx = {
      get(service) {
        if (service === "agentDefaultModel") return { currentSelection: () => ({ provider: "p", model: "m" }) };
        if (service === "agents") return { async create() { return { agent: fakeAgent(sent), dispose: async () => {} }; } };
        throw new Error(`unexpected ctx.get: ${service}`);
      },
    };
    const port = 9200 + Math.floor(Math.random() * 90);
    const server = startServer(ctx, port);
    await new Promise((resolve) => setTimeout(resolve, 200));
    try {
      await fetch(`http://127.0.0.1:${port}/turn`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer focus-test-token" },
        body: JSON.stringify({ message: "今天有哪些待办", session_id: "asa-focus-2", context: { type: "page", page: "agent" } }),
      }).then((res) => res.text());
      assert.equal(sent.length, 1);
      assert.equal(sent[0], "今天有哪些待办");
    } finally {
      server.close();
    }
  });
});
