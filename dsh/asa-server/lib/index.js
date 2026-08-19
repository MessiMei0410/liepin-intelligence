import { randomUUID, timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";
import http from "node:http";
import { homedir } from "node:os";
import { installModelSelection } from "@deepseek-ai/dsh-agent";
import { createUserMessage } from "@deepseek-ai/dsh-llm";
import { SessionId } from "@deepseek-ai/dsh-session";
import { createObjectRefCollector } from "./object-actions.js";
import { delegateDoneFields } from "./copilot-payload.js";

/**
 * @asa/dsh-asa-server — ASA 常驻 Agent 服务器（resident runner）。
 * 与 dsh-headless 的 one-shot runner 同源：都用 agents/sessions/agentDefaultModel
 * 跑 agent 循环，但不 exit，而是起一个 node:http 服务器：
 *   POST /turn {message, session_id?} -> SSE 流（text 增量 + card + done），会话复用
 *   GET  /health                     -> {ok}
 * 流式：订阅 agent 会话的 session/event 火线，把 assistant/chunk(text-delta) 实时转成
 * SSE `text` 事件、assistant/chunk(reasoning-delta) 转成 SSE `thinking` 事件（思考过程透传）；
 * tool/result meta 里的 action_card 转成 SSE `card` 事件、confirm_request
 * 转成 SSE `confirm_request` 事件（写确认卡）；轮结束发 `done`。
 * text/thinking 增量走时间窗聚合（createDeltaAggregator）：token 级小 chunk 不逐条发 SSE
 * （前端每事件一次 setState + markdown 全量重解析，高频重渲染肉眼卡顿），攒够
 * maxChars 或 windowMs 到点 flush 一次；card/confirm_request/progress/done 前强制 flush
 * 保序，这些事件本身的实时性不受聚合影响。
 * done 除 session_id/answer/ok/error 外，还聚合本轮工具结果 meta.object_refs 里的业务对象
 * （asa-tools 投影的工作流/候选人/岗位 ID）为 suggested_actions（操作芯片：打开工作流/
 * 人选/岗位弹窗，≤4）与 references（对象卡，≤8），空则不携带。同 session_id 复用 live agent（多轮记忆）。
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
  asa_candidate_preflight: "候选人操作预检（发起界面确认）",
  asa_approval_preflight: "审批决定预检（发起界面确认）",
  asa_workflow_action_preflight: "工作流动作预检（发起界面确认）",
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

// text/thinking 增量聚合窗口：攒够 maxChars 立即 flush，否则 windowMs 到点 flush。
const TEXT_FLUSH_WINDOW_MS = Number(process.env.ASA_DSH_TEXT_FLUSH_WINDOW_MS || 50);
const TEXT_FLUSH_MAX_CHARS = Number(process.env.ASA_DSH_TEXT_FLUSH_MAX_CHARS || 24);

/** assistant/chunk → [SSE 事件名, 文本]：text-delta→text、reasoning-delta→thinking。
 *  其余 chunk（tool-call-delta/block-start 等）与空文本返回 null。纯函数导出以便单测。 */
function chunkSseDelta(chunk) {
  if (!chunk || typeof chunk.text !== "string" || chunk.text === "") return null;
  if (chunk.type === "text-delta") return ["text", chunk.text];
  if (chunk.type === "reasoning-delta") return ["thinking", chunk.text];
  return null;
}

/** text/thinking 增量聚合器：token 级小 chunk 合并成低频 SSE 事件，降低前端重渲染频率。
 *  push() 累积；满 maxChars 立即 flush 该类型，否则开一个 windowMs 定时器 flush 全部。
 *  其他 SSE 事件（card/confirm_request/progress/done）写出前必须 flush() 保序。 */
function createDeltaAggregator(write, { windowMs = TEXT_FLUSH_WINDOW_MS, maxChars = TEXT_FLUSH_MAX_CHARS } = {}) {
  const pending = { text: "", thinking: "" };
  let timer = null;
  const flushType = (type) => {
    if (pending[type] === "") return;
    write(type, { content: pending[type] });
    pending[type] = "";
  };
  const flush = () => {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    flushType("text");
    flushType("thinking");
  };
  const push = (type, text) => {
    pending[type] += text;
    if (pending[type].length >= maxChars) {
      flushType(type);
      return;
    }
    if (!timer) {
      timer = setTimeout(flush, windowMs);
      timer.unref?.();
    }
  };
  return { push, flush };
}

/** tool/call → SSE progress 文案（导出以便单测）。 */
function toolCallProgressMessage(toolName) {
  return `${TOOL_LABELS[toolName] || `调用工具 ${toolName}`}…`;
}

// tool/result 后的进度文案：工具结果已回、LLM 接续生成，让「死寂段」持续可见。
const TOOL_RESULT_PROGRESS_MESSAGE = "整理工具结果，生成中…";

/** tool/result 事件 data → 待透传的 SSE 事件列表（card / confirm_request）。
 *  纯函数导出以便 node:test 单测。confirm_request 必须带 preflight_token 才算有效。 */
function toolResultSseEvents(data) {
  const events = [];
  const meta = data && data.meta;
  const card = meta && meta.action_card;
  if (card && typeof card === "object") events.push(["card", card]);
  const confirm = meta && meta.confirm_request;
  if (confirm && typeof confirm === "object" && confirm.preflight_token) events.push(["confirm_request", confirm]);
  return events;
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
    // 本轮业务对象收集器：tool/result meta.object_refs（asa-tools 投影的工作流/
    // 候选人/岗位 ID）轮末聚合成 suggested_actions/references 随 done 下发。
    const objectRefs = createObjectRefCollector();
    // 本轮 Copilot 委托载荷：asa_copilot_ask 把 Copilot 脑 done 的结构化字段投到
    // meta.copilot_payload（理解卡/执行回执/工作流进度原料/焦点/模型参与/复数卡片/
    // 上下文），轮末组装成 workflow_progress 等并入 done（与 Copilot 脑直连同形）。
    // 一轮可能多次委托，最后一次的载荷生效（与 action_card 单卡语义一致）。
    let copilotPayload = null;

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

    // text/thinking 增量聚合：token 级小 chunk 合并成低频 SSE，前端重渲染频率随之下降。
    // card/confirm_request/progress/done 等其他事件写出前先 flush 保序（实时性不受影响）。
    const deltas = createDeltaAggregator((type, data) => writeSse(res, type, data));

    // 订阅本 agent 会话的事件火线：text-delta/reasoning-delta 实时转 SSE，同时增量聚合最终答案，
    // 不再每轮全量扫描 session.events（长会话 O(n²)）。
    const dispose = agent.ctx.on("session/event", (session, event) => {
      if (event.seq < firstSeq) return;
      if (event.type === "assistant/chunk") {
        const delta = chunkSseDelta(event.data && event.data.chunk);
        if (delta) deltas.push(delta[0], delta[1]);
      } else if (event.type === "assistant/message") {
        const content = event.data && event.data.message && event.data.message.content;
        if (Array.isArray(content)) {
          const joined = content.filter((block) => block.type === "text").map((block) => block.text).join("");
          if (joined !== "") answer = joined;
        }
      } else if (event.type === "tool/call") {
        // 工具执行段本无任何输出（「死寂」），转发为进度事件让等待可见。
        const toolName = event.data && event.data.name;
        if (toolName) {
          deltas.flush();
          writeSse(res, "progress", { message: toolCallProgressMessage(toolName) });
        }
      } else if (event.type === "tool/result") {
        // 结构化透传：presentationMeta 把 action_card（名单卡等）与 confirm_request
        // （写确认卡：preflight_token + 动作摘要 + 对象信息）挂到 tool/result meta
        // （完整 JSON 快照，不受 render 16k 截断影响）。前端收到 card 事件后挂到本轮
        // assistant 消息；收到 confirm_request 事件后渲染确认卡，用户确认后由前端调
        // Core activate + 写端点完成写入。
        deltas.flush();
        for (const [type, payload] of toolResultSseEvents(event.data)) writeSse(res, type, payload);
        // 工具结果已回、LLM 接续生成：补一条进度，让结果回传到首个 text/thinking
        // 之间的静默段也有状态可见（前端在文本到达前保持 AgentThinking 动画）。
        writeSse(res, "progress", { message: TOOL_RESULT_PROGRESS_MESSAGE });
        // 对象操作入口：asa-tools 只读工具把结果里的业务对象 ID 投到 meta.object_refs，
        // 轮末聚合成 suggested_actions/references（「都打开我看下」场景的点击入口）。
        objectRefs.add(event.data && event.data.meta && event.data.meta.object_refs);
        // Copilot 委托载荷：轮末并入 done（前端按 Copilot 同形字段渲染）。
        // tool/result data 没有顶层 name（name 在 tool/call 上）；copilot_payload
        // 只有 asa_copilot_ask 投影，凭键存在即可归属。
        const delegatePayload = event.data && event.data.meta && event.data.meta.copilot_payload;
        if (delegatePayload && typeof delegatePayload === "object") {
          copilotPayload = delegatePayload;
        }
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
      const { suggested_actions, references } = objectRefs.outputs();
      // 轮末强制 flush：聚合窗口里残留的 text/thinking 必须先于 done 下发保序。
      deltas.flush();
      writeSse(res, "done", {
        session_id: sessionId,
        answer,
        ok: reason?.kind === "completed",
        ...(suggested_actions.length ? { suggested_actions } : {}),
        ...(references.length ? { references } : {}),
        // Copilot 委托载荷并入 done：understanding_card/execution_receipt/business_focus/
        // model_participation/action_cards/context 原样透传，workflow 原料组装为
        // workflow_progress（buildWorkflowProgress，与 Core bridge 同形）。
        ...delegateDoneFields(copilotPayload),
        error: reason?.kind === "error"
          ? reason.error.message
          : reason?.kind === "aborted"
            ? `turn aborted (${reason.reason?.reason || reason.reason?.kind || "unknown"})`
            : void 0,
      });
    } finally {
      finished = true;
      clearTimeout(timeout);
      // 聚合定时器必须随轮次回收（flush 幂等；res 已结束时 writeSse 静默丢弃）。
      deltas.flush();
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

export { apply, inject, name, toolResultSseEvents, chunkSseDelta, createDeltaAggregator, toolCallProgressMessage, TOOL_RESULT_PROGRESS_MESSAGE };
