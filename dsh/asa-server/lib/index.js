import { randomUUID, timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";
import http from "node:http";
import { homedir } from "node:os";
import { installModelSelection } from "@deepseek-ai/dsh-agent";
import { createUserMessage } from "@deepseek-ai/dsh-llm";
import { SessionId } from "@deepseek-ai/dsh-session";

/**
 * @asa/dsh-asa-server — ASA 常驻 Agent 服务器（resident runner）。
 * 与 dsh-headless 的 one-shot runner 同源：都用 agents/sessions/agentDefaultModel
 * 跑 agent 循环，但不 exit，而是起一个 node:http 服务器：
 *   POST /turn {message, session_id?} -> SSE 流（text 增量 + card + done），会话复用
 *   GET  /health                     -> {ok}
 * 流式：订阅 agent 会话的 session/event 火线，把 assistant/chunk(text-delta) 实时转成
 * SSE `text` 事件；tool/result meta 里的 action_card 转成 SSE `card` 事件；轮结束发 `done`。同 session_id 复用 live agent（多轮记忆）。
 *
 * v1.3 加固（审计后）：
 * - 事件订阅在 finally 中 dispose，异常路径不再残留监听器（此前会叠加重复推流）。
 * - Agent 池带句柄：空闲 TTL（默认 30 分钟）+ 上限 LRU 回收，常驻不再只增不减。
 * - 客户端断连（前端 AbortController）即时 cancel 本轮，释放会话队列、止损 LLM 调用。
 * - 单轮总超时（默认 300s，ASA_DSH_TURN_TIMEOUT_MS），超时 cancel 并回 done ok:false。
 * - 请求体上限（默认 1MB，ASA_DSH_MAX_BODY_BYTES），超限 413。
 * - token 比较改恒定时间（timingSafeEqual）。
 * - CORS 从 `*` 收紧为白名单回显（Core 8765 + vite dev 5173）。
 * - 会话串行队列在队尾排空后删除条目，Map 不再无界增长。
 * - 优雅停机：SIGTERM/SIGINT 停接新连接、取消在跑轮次、dispose 全部 agent 后退出（10s 硬退兜底）。
 */

const name = "asa-resident-runner";
const inject = ["agentDefaultModel", "agents", "sessions"];

const PORT = Number(process.env.ASA_DSH_RESIDENT_PORT || 8891);
const TURN_TIMEOUT_MS = Number(process.env.ASA_DSH_TURN_TIMEOUT_MS || 300_000);
const MAX_BODY_BYTES = Number(process.env.ASA_DSH_MAX_BODY_BYTES || 1_048_576);
const AGENT_IDLE_TTL_MS = Number(process.env.ASA_DSH_AGENT_IDLE_TTL_MS || 30 * 60 * 1000);
const MAX_AGENTS = Number(process.env.ASA_DSH_MAX_AGENTS || 20);

// CORS 白名单：ASA app / 浏览器经 Core(8765) 加载的前端，以及 vite dev(5173)。
// 非浏览器客户端（curl）不带 Origin，不受 CORS 约束；未知 Origin 的浏览器预检直接失败。
const ALLOWED_ORIGINS = new Set([
  "http://127.0.0.1:8765",
  "http://localhost:8765",
  "http://127.0.0.1:5173",
  "http://localhost:5173",
]);

// 鉴权：本地共享密钥（0600），Core 下发、前端带 Bearer。空 = 未启用鉴权（dev 回退）。
const TOKEN = process.env.ASA_DSH_TOKEN || (() => {
  try {
    return readFileSync(process.env.ASA_DSH_TOKEN_FILE || `${homedir()}/.dsh/asa-bridge-token`, "utf8").trim();
  } catch {
    return "";
  }
})();

/** 恒定时间比较 Bearer token，避免时序侧信道。 */
function tokenOk(header) {
  if (!TOKEN) return true;
  const actual = Buffer.from(String(header || ""));
  const expected = Buffer.from(`Bearer ${TOKEN}`);
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}

// 工具调用的前端进度文案（tool/call → SSE progress）：让「死寂」的工具执行段可见。
const TOOL_LABELS = {
  asa_dashboard: "读取工作台总览",
  asa_jobs: "查询岗位列表",
  asa_candidates: "查询候选人",
  asa_workflow: "查询工作流",
  asa_candidate_preflight: "候选人操作预检",
  asa_candidate_commit: "提交候选人操作",
  asa_approval_decision: "提交审批决定",
  asa_copilot_ask: "委托 Copilot 做领域分析",
};

function corsHeaders(req) {
  const headers = {
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
  };
  const origin = req.headers.origin;
  if (origin && ALLOWED_ORIGINS.has(origin)) headers["Access-Control-Allow-Origin"] = origin;
  return headers;
}

function writeJson(res, req, code, obj) {
  res.writeHead(code, { "Content-Type": "application/json; charset=utf-8", ...corsHeaders(req) });
  res.end(JSON.stringify(obj));
}

function writeSse(res, type, data) {
  if (res.writableEnded || res.destroyed) return;
  res.write(`event: ${type}\n`);
  res.write(`data: ${JSON.stringify(data)}\n\n`);
}

async function readBody(req) {
  let body = "";
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) {
      const error = new Error("payload too large");
      error.statusCode = 413;
      throw error;
    }
    body += chunk;
  }
  return body;
}

function apply(ctx) {
  // Agent 池：sessionId -> { handle, agent, busy, evictTimer }。
  // create() 返回的 handle 带 dispose 能力（停 loop、注销 agent、清 session），必须持有。
  const pool = new Map();

  function touch(sessionId, entry) {
    if (entry.evictTimer) clearTimeout(entry.evictTimer);
    entry.evictTimer = setTimeout(() => void evict(sessionId), AGENT_IDLE_TTL_MS);
    entry.evictTimer.unref?.();
  }

  async function evict(sessionId) {
    const entry = pool.get(sessionId);
    if (!entry) return;
    if (entry.busy) {
      // 轮次进行中不回收，重新计时；轮结束 touch 会再续命。
      touch(sessionId, entry);
      return;
    }
    pool.delete(sessionId);
    try {
      await entry.handle.dispose();
    } catch (error) {
      console.warn(`[asa-resident] dispose agent ${sessionId} failed:`, error);
    }
  }

  function evictOldestIdle() {
    for (const [sessionId, entry] of pool) {
      if (!entry.busy) {
        void evict(sessionId);
        return;
      }
    }
  }

  /** 取 live agent，没有则按默认模型新建（agent.id === session.id，故按 sessionId 可查回）。 */
  async function ensureAgent(sessionId) {
    const existing = pool.get(sessionId);
    if (existing) return existing;
    if (pool.size >= MAX_AGENTS) evictOldestIdle();
    const defaultModel = ctx.get("agentDefaultModel");
    const selection = defaultModel.currentSelection();
    const handle = await ctx.get("agents").create({
      sessionId: SessionId(sessionId),
      meta: { cwd: process.cwd() },
      agentOptions: { provider: selection.provider, model: selection.model },
      setup: (agentCtx) => {
        installModelSelection(agentCtx, { current: selection, assembled: void 0 });
      },
    });
    await handle.agent.whenIdle();
    const entry = { handle, agent: handle.agent, busy: false, evictTimer: null };
    pool.set(sessionId, entry);
    touch(sessionId, entry);
    return entry;
  }

  // 每会话串行化：并发 /turn 到同一 session 时排队，避免 followup 交错污染会话状态。
  // 队尾排空后删除条目，Map 不随会话数无界增长。
  const turnQueues = new Map();
  const serializeTurn = (sessionId, fn) => {
    const prev = turnQueues.get(sessionId) || Promise.resolve();
    const next = prev.then(fn, fn);
    const tail = next.catch(() => {});
    turnQueues.set(sessionId, tail);
    void tail.then(() => {
      if (turnQueues.get(sessionId) === tail) turnQueues.delete(sessionId);
    });
    return next;
  };

  /** 跑一轮：订阅事件火线实时转 SSE，断连/超时即时 cancel，监听器 finally 回收。 */
  async function runTurn(req, res, sessionId, message) {
    const entry = await ensureAgent(sessionId);
    entry.busy = true;
    const agent = entry.agent;
    const firstSeq = agent.session.seq;
    const startedAt = Date.now();
    let answer = "";
    let reason;
    let finished = false;

    // 客户端断连（前端 abort / 关页）：取消本轮，止损并尽早释放会话队列。
    const onClose = () => {
      if (finished) return;
      try {
        agent.cancel({ kind: "hook", reason: "client-disconnect" });
      } catch { /* agent 可能已 dispose */ }
    };
    res.on("close", onClose);

    const timeout = setTimeout(() => {
      try {
        agent.cancel({ kind: "hook", reason: "turn-timeout" });
      } catch { /* agent 可能已 dispose */ }
    }, TURN_TIMEOUT_MS);
    timeout.unref?.();

    // 订阅本 agent 会话的事件火线：text-delta 实时转 SSE，同时增量聚合最终答案，
    // 不再每轮全量扫描 session.events（长会话 O(n²)）。
    const dispose = agent.ctx.on("session/event", (session, event) => {
      if (event.seq < firstSeq) return;
      if (event.type === "assistant/chunk") {
        const chunk = event.data && event.data.chunk;
        if (chunk && chunk.type === "text-delta" && typeof chunk.text === "string" && chunk.text !== "") {
          writeSse(res, "text", { content: chunk.text });
        }
      } else if (event.type === "assistant/message") {
        const content = event.data && event.data.message && event.data.message.content;
        if (Array.isArray(content)) {
          const joined = content.filter((block) => block.type === "text").map((block) => block.text).join("");
          if (joined !== "") answer = joined;
        }
      } else if (event.type === "tool/call") {
        // 工具执行段本无任何输出（「死寂」），转发为进度事件让等待可见。
        const toolName = event.data && event.data.name;
        if (toolName) writeSse(res, "progress", { message: `${TOOL_LABELS[toolName] || `调用工具 ${toolName}`}…` });
      } else if (event.type === "tool/result") {
        // 结构化卡片透传：asa_copilot_ask 经 presentationMeta 把 Copilot 的
        // action_card（候选人名单卡等）挂到 tool/result 的 meta（完整 JSON 快照，
        // 不受 render 16k 截断影响）。前端收到 card 事件后挂到本轮 assistant 消息。
        const card = event.data && event.data.meta && event.data.meta.action_card;
        if (card && typeof card === "object") writeSse(res, "card", card);
      } else if (event.type === "turn/end") {
        reason = event.data && event.data.reason;
      }
    });

    try {
      writeSse(res, "progress", { message: "DSH 编排中…" });
      agent.followup(
        createUserMessage({
          content: [{ type: "text", text: message }],
          source: { kind: "user" },
        }),
      );
      await agent.whenIdle();
      writeSse(res, "done", {
        session_id: sessionId,
        answer,
        ok: reason?.kind === "completed",
        error: reason?.kind === "error"
          ? reason.error.message
          : reason?.kind === "aborted"
            ? `turn aborted (${reason.reason?.reason || reason.reason?.kind || "unknown"})`
            : void 0,
      });
    } finally {
      finished = true;
      clearTimeout(timeout);
      res.off("close", onClose);
      dispose();
      entry.busy = false;
      if (pool.has(sessionId)) touch(sessionId, entry);
      // 每轮一行观测日志（session/结果/答案长度/耗时），用于排查截断与卡轮。
      console.log(`[asa-resident] turn session=${sessionId} ok=${reason?.kind === "completed"} reason=${reason?.kind || "unknown"} answer_chars=${answer.length} ms=${Date.now() - startedAt}`);
    }
  }

  const server = http.createServer(async (req, res) => {
    if (req.method === "OPTIONS") {
      res.writeHead(204, corsHeaders(req));
      res.end();
      return;
    }
    if (req.method === "GET" && req.url === "/health") {
      writeJson(res, req, 200, { ok: true, profile: "asa-server", sessions: pool.size });
      return;
    }
    if (req.method === "POST" && req.url === "/turn") {
      if (!tokenOk(req.headers.authorization)) {
        writeJson(res, req, 401, { ok: false, error: "unauthorized" });
        return;
      }
      let payload = {};
      try {
        payload = JSON.parse((await readBody(req)) || "{}");
      } catch (error) {
        writeJson(res, req, error.statusCode || 400, { ok: false, error: error.statusCode ? "payload too large" : "bad json" });
        return;
      }
      const message = String(payload.message || "").trim();
      const sessionId = String(payload.session_id || `asa-${randomUUID()}`);
      if (!message) {
        writeJson(res, req, 400, { ok: false, error: "message is required" });
        return;
      }
      res.writeHead(200, {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
        ...corsHeaders(req),
      });
      try {
        await serializeTurn(sessionId, () => runTurn(req, res, sessionId, message));
      } catch (error) {
        writeSse(res, "done", {
          session_id: sessionId,
          answer: "",
          ok: false,
          error: error instanceof Error ? error.message : String(error),
        });
      }
      if (!res.writableEnded) res.end();
      return;
    }
    writeJson(res, req, 404, { ok: false, error: "not found" });
  });

  server.listen(PORT, "127.0.0.1", () => {
    console.log(`[asa-resident] http://127.0.0.1:${PORT}`);
  });

  // 优雅停机：launchd kickstart / kill 发 SIGTERM。停接新连接 → 取消在跑轮次 →
  // dispose 全部 agent（停 loop、清 session）→ 退出。dispose 可能 hang，
  // 10s 硬退兜底，避免 launchd 反复 kickstart 失败。
  let shuttingDown = false;
  async function shutdown(signal) {
    if (shuttingDown) return;
    shuttingDown = true;
    console.log(`[asa-resident] ${signal} received, draining ${pool.size} agent(s)`);
    const hardExit = setTimeout(() => {
      console.warn("[asa-resident] graceful shutdown timed out, hard exit");
      process.exit(1);
    }, 10_000);
    hardExit.unref?.();
    server.close();
    const entries = [...pool.values()];
    pool.clear();
    await Promise.allSettled(
      entries.map(async (entry) => {
        if (entry.evictTimer) clearTimeout(entry.evictTimer);
        try {
          entry.agent.cancel({ kind: "hook", reason: `shutdown-${signal.toLowerCase()}` });
        } catch { /* agent 可能已 dispose */ }
        try {
          await entry.handle.dispose();
        } catch (error) {
          console.warn("[asa-resident] dispose on shutdown failed:", error);
        }
      }),
    );
    clearTimeout(hardExit);
    process.exit(0);
  }
  process.on("SIGTERM", () => void shutdown("SIGTERM"));
  process.on("SIGINT", () => void shutdown("SIGINT"));
}

export { apply, inject, name };
