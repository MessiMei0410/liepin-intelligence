import { randomUUID, timingSafeEqual } from "node:crypto";
import { existsSync, readFileSync, renameSync } from "node:fs";
import http from "node:http";
import { homedir } from "node:os";
import { join } from "node:path";
import { installModelSelection } from "@deepseek-ai/dsh-agent";
import { createUserMessage } from "@deepseek-ai/dsh-llm";
import { SessionId } from "@deepseek-ai/dsh-session";
import { createObjectRefCollector } from "./object-actions.js";
import { delegateDoneFields } from "./copilot-payload.js";
import { createSubagentTracker } from "./subagent-events.js";

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
 * 子代理委派（subagent/subagent_fork 工具）的生命周期经 agent.ctx 上的 Cordis
 * `subagent/start|end` 事件转成 SSE `subagent` 增量事件（start/end），终态数组随 done 下发。
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

const residentPort = () => Number(process.env.ASA_DSH_RESIDENT_PORT || 8891);
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
  asa_candidate_profile: "读取人选完整档案",
  asa_workflow: "查询工作流",
  asa_candidate_preflight: "候选人操作预检（发起界面确认）",
  asa_dedupe_scan: "扫描疑似重复人选",
  asa_pool_filter: "生成/筛选岗位候选名单",
  asa_approval_preflight: "审批决定预检（发起界面确认）",
  asa_workflow_action_preflight: "工作流动作预检（发起界面确认）",
  asa_resume_backfill: "简历回填预检（发起界面确认）",
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

const INTERMEDIATE_ANSWER_PATTERNS = [
  /子代理仍在执行/,
  /子代理(?:还在|正在)执行/,
  /等(?:它|其|子代理)返回/,
  /稍后(?:给出|提供)(?:最终|完整)/,
  /等待(?:子代理|它|其)返回/,
];

/** 识别“仍在执行/等待返回”中间答复；只有配合名单卡才触发收尾。 */
function isIntermediateAnswer(answer) {
  const text = typeof answer === "string" ? answer.trim() : "";
  return Boolean(text) && INTERMEDIATE_ANSWER_PATTERNS.some(pattern => pattern.test(text));
}

function shouldFollowupCandidateList(answer, hasCandidateListCard) {
  return hasCandidateListCard === true && isIntermediateAnswer(answer);
}

function mergeCopilotPayload(previous, next) {
  if (!previous || typeof previous !== "object") return next;
  if (!next || typeof next !== "object") return previous;
  const merged = { ...previous };
  for (const [key, value] of Object.entries(next)) {
    if (value !== undefined) merged[key] = value;
  }
  for (const key of ["understanding_card", "execution_receipt", "analysis_card", "business_focus", "model_participation", "context"]) {
    if (!next[key] || typeof next[key] !== "object" || Array.isArray(next[key])) {
      if (previous[key] !== undefined) merged[key] = previous[key];
      else delete merged[key];
    }
  }
  const previousCards = Array.isArray(previous.action_cards) ? previous.action_cards : [];
  const nextCards = Array.isArray(next.action_cards) ? next.action_cards : [];
  const previousCandidateCard = previousCards.find(card => card && card.type === "candidate_list");
  if (!nextCards.length) merged.action_cards = previousCards;
  else if (previousCandidateCard && !nextCards.some(card => card && card.type === "candidate_list")) {
    merged.action_cards = [previousCandidateCard, ...nextCards];
  }
  return merged;
}

const CANDIDATE_LIST_FOLLOWUP_PROMPT = "基于本轮已经返回的真实候选名单工具证据完成收尾：不要再返回“子代理仍在执行”“等待返回”或“稍后给最终结果”等中间态。请直接给出已确认、相邻经验、待核验/不满足的分档判断、推荐顺序、证据依据和下一步；如果用户没有提出筛选条件，就简要说明名单已生成及可执行的下一步。不要重复调用名单工具。";
const CANDIDATE_LIST_FOLLOWUP_INCOMPLETE = "名单已生成，但本轮分析收尾仍未完成，暂未形成可靠的分档推荐结论；请重新发起分析或指定要核验的候选人。";

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

// ---------------------------------------------------------------------------
// 会话碰撞自愈：陈旧持久化日志定位（路径算法逐行复刻自
// @deepseek-ai/dsh-session-persistence-jsonl 的 encodeSegment/projectKey，
// 该包未导出这些工具函数；ASA 的 session id 只含安全字符，实际不会走转义分支）。
// ---------------------------------------------------------------------------
const dshSessionsRoot = () => process.env.ASA_DSH_SESSIONS_ROOT
  || `${process.env.DSH_HOME || `${homedir()}/.dsh`}/sessions`;

function encodeSegment(raw) {
  if (raw === ".") return "~002E";
  if (raw === "..") return "~002E~002E";
  let out = "";
  for (let i = 0; i < raw.length; i++) {
    const code = raw.charCodeAt(i);
    const ch = String.fromCharCode(code);
    if (ch !== "~" && /^[A-Za-z0-9._-]$/.test(ch)) out += ch;
    else out += `~${code.toString(16).toUpperCase().padStart(4, "0")}`;
  }
  return out;
}

function projectKey(cwd) {
  let readable = "";
  let separatorRun = false;
  for (let i = 0; i < cwd.length; i++) {
    const code = cwd.charCodeAt(i);
    const ch = String.fromCharCode(code);
    if (ch === "/" || ch === "\\" || ch === ":") {
      if (!separatorRun) readable += "-";
      separatorRun = true;
    } else if (ch !== "~" && /^[A-Za-z0-9._-]$/.test(ch)) {
      readable += ch;
      separatorRun = false;
    } else {
      readable += `~${code.toString(16).toUpperCase().padStart(4, "0")}`;
      separatorRun = false;
    }
  }
  return `--${(readable.replace(/^-+/, "") || "root").slice(0, 251)}--`;
}

/** 把某会话的持久化事件日志挪到 .bak-<ts>（不存在则空操作），供碰撞自愈重试。 */
function archiveStaleSessionLog(sessionId) {
  const log = join(join(dshSessionsRoot(), projectKey(process.cwd())), encodeSegment(sessionId), "session.jsonl.zstd");
  if (existsSync(log)) renameSync(log, `${log}.bak-${Date.now()}`);
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
    const createAgent = () =>
      ctx.get("agents").create({
        sessionId: SessionId(sessionId),
        meta: { cwd: process.cwd() },
        agentOptions: { provider: selection.provider, model: selection.model },
        setup: (agentCtx) => {
          installModelSelection(agentCtx, { current: selection, assembled: void 0 });
        },
      });
    let handle;
    try {
      handle = await createAgent();
      await handle.agent.whenIdle();
    } catch (error) {
      // 会话碰撞自愈（2026-08-19 实证）：部署重启恰逢在跑轮次时，旧 agent 被取消
      // 会在磁盘留下与新 agent 事件流不匹配的持久化日志，DSH 会话存储拒绝收养
      // （id collision），此后该会话 id 每轮毫秒级失败。碰撞可能在 create 或
      // 异步收养（whenIdle）阶段抛出，两处都要兜住。归档陈旧日志并重试一次；
      // 界面历史由 Core 回填承载，DSH 侧仅丢失该线程的工作记忆。
      if (!/id collision/.test(String(error && error.message))) throw error;
      archiveStaleSessionLog(sessionId);
      console.warn(`[asa-resident] session ${sessionId} id collision，已归档陈旧日志并重试`);
      handle = await createAgent();
      await handle.agent.whenIdle();
    }
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

  /** 丢弃会话的池内 agent（碰撞自愈用）：dispose 后下次 ensureAgent 全新创建。 */
  async function discardAgent(sessionId) {
    const entry = pool.get(sessionId);
    if (!entry) return;
    pool.delete(sessionId);
    if (entry.evictTimer) clearTimeout(entry.evictTimer);
    try {
      await entry.handle.dispose();
    } catch { /* agent 可能已 dispose */ }
  }

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
    // 本轮已投影 candidate_list action_card（工具 meta.action_card 或委托载荷
    // action_cards）：轮末抑制 candidate references/open_candidate 芯片（名单卡自带入口）。
    let sawCandidateListCard = false;
    // 本轮 Copilot 委托载荷：asa_copilot_ask 把 Copilot 脑 done 的结构化字段投到
    // meta.copilot_payload（理解卡/执行回执/工作流进度原料/焦点/模型参与/复数卡片/
    // 上下文），轮末组装成 workflow_progress 等并入 done（与 Copilot 脑直连同形）。
    // 一轮可能多次委托，最后一次的载荷生效（与 action_card 单卡语义一致）。
    let copilotPayload = null;
    // 本轮子代理生命周期聚合：agent.ctx 上的 subagent/start|end（dsh-subagent 按派生父
    // agent scope 分发，本 agent 的委派都能收到）转成 SSE `subagent` 增量事件，轮末
    // 终态数组随 done 下发（前端渲染「子代理执行」卡片并回填）。
    const subagents = createSubagentTracker();

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
          // 子代理委派工具（subagent/subagent_fork）：登记描述，供 subagent/start 配对 label。
          subagents.noteToolCall(toolName, event.data && event.data.arguments);
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
        const projectedCard = event.data && event.data.meta && event.data.meta.action_card;
        if (projectedCard && projectedCard.type === "candidate_list") sawCandidateListCard = true;
        // Copilot 委托载荷：轮末并入 done（前端按 Copilot 同形字段渲染）。
        // tool/result data 没有顶层 name（name 在 tool/call 上）；copilot_payload
        // 只有 asa_copilot_ask 投影，凭键存在即可归属。
        const delegatePayload = event.data && event.data.meta && event.data.meta.copilot_payload;
        if (delegatePayload && typeof delegatePayload === "object") {
          copilotPayload = mergeCopilotPayload(copilotPayload, delegatePayload);
        }
      } else if (event.type === "turn/end") {
        reason = event.data && event.data.reason;
      }
    });

    // 子代理生命周期（dsh-subagent 的 Cordis 事件，按派生父 agent scope 分发——
    // agent.ctx 监听即只收到本 agent 的委派，无需全局订阅）。label 优先读子 session 的
    // subagent/descriptor（durable label），读不到退 tool/call 描述按序兜底。
    const readDescriptorLabel = (childId) => {
      try {
        const child = ctx.get("agents").get(childId);
        const descriptor = child && child.session && Array.isArray(child.session.events)
          ? child.session.events.find((item) => item.type === "subagent/descriptor")
          : null;
        return descriptor && descriptor.data && typeof descriptor.data.label === "string" ? descriptor.data.label : "";
      } catch {
        return "";
      }
    };
    const disposeSubagentStart = agent.ctx.on("subagent/start", (info) => {
      const data = subagents.start(info, readDescriptorLabel(info && info.id));
      if (data) {
        deltas.flush();
        writeSse(res, "subagent", data);
      }
    });
    const disposeSubagentEnd = agent.ctx.on("subagent/end", (info) => {
      const data = subagents.end(info);
      if (data) {
        deltas.flush();
        writeSse(res, "subagent", data);
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
      const firstAnswer = answer;
      const firstDelegatePayload = copilotPayload;
      const firstDelegateCards = firstDelegatePayload && Array.isArray(firstDelegatePayload.action_cards)
        ? firstDelegatePayload.action_cards
        : [];
      const hasCandidateListCard = sawCandidateListCard
        || firstDelegateCards.some((card) => card && card.type === "candidate_list");
      if (shouldFollowupCandidateList(firstAnswer, hasCandidateListCard)) {
        // 名单工具已经给出真实证据时，补一次同会话收尾；不再调用名单工具，避免重复查询。
        deltas.flush();
        writeSse(res, "progress", { message: "基于名单证据整理最终判断…" });
        try {
          agent.followup(createUserMessage({
            content: [{ type: "text", text: CANDIDATE_LIST_FOLLOWUP_PROMPT }],
            source: { kind: "user" },
          }));
          await agent.whenIdle();
          if (isIntermediateAnswer(answer)) {
            // 只允许一次收尾；二次仍是中间态时止损，不能递归调用或把等待文案当结论。
            answer = CANDIDATE_LIST_FOLLOWUP_INCOMPLETE;
          }
        } catch (error) {
          // 收尾失败时保留首轮名单卡和答案，避免把已有真实证据变成错误态。
          console.warn(`[asa-resident] candidate-list followup failed: ${error instanceof Error ? error.message : String(error)}`);
          answer = CANDIDATE_LIST_FOLLOWUP_INCOMPLETE;
          copilotPayload = firstDelegatePayload;
        }
      }
      const delegateCards = copilotPayload && Array.isArray(copilotPayload.action_cards) ? copilotPayload.action_cards : [];
      const finalHasCandidateListCard = hasCandidateListCard
        || delegateCards.some((card) => card && card.type === "candidate_list");
      const { suggested_actions, references } = objectRefs.outputs({ answer, candidateListCard: finalHasCandidateListCard });
      // 轮末强制 flush：聚合窗口里残留的 text/thinking 必须先于 done 下发保序。
      deltas.flush();
      // 子代理终态快照：本轮派生的子代理（含仍 running 的后台委派）随 done 下发，
      // 前端渲染终态卡片并随 record-turn 回填。
      const subagentRuns = subagents.list();
      writeSse(res, "done", {
        session_id: sessionId,
        answer,
        ok: reason?.kind === "completed",
        ...(subagentRuns.length ? { subagents: subagentRuns } : {}),
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
      disposeSubagentStart();
      disposeSubagentEnd();
      entry.busy = false;
      if (pool.has(sessionId)) touch(sessionId, entry);
      // 每轮一行观测日志（session/结果/答案长度/耗时），用于排查截断与卡轮。
      console.log(`[asa-resident] turn session=${sessionId} ok=${reason?.kind === "completed"} reason=${reason?.kind || "unknown"} answer_chars=${answer.length} ms=${Date.now() - startedAt}`);
    }
  }

  /** turn 级碰撞自愈：碰撞可能在 ensureAgent / followup / whenIdle 任一阶段抛出
   *  （DSH 持久化日志的收养是惰性的，create 成功不代表收养成功——2026-08-19
   *  连续三个会话实证 ensureAgent 级兜底接不住）。整轮重试一次：丢弃池内 agent、
   *  归档陈旧日志、重跑。progress 事件在重试时会重复一行，无害。 */
  async function runTurnHealed(req, res, sessionId, message) {
    try {
      await runTurn(req, res, sessionId, message);
    } catch (error) {
      if (!/id collision/.test(String(error && error.message))) throw error;
      console.warn(`[asa-resident] session ${sessionId} id collision（turn 级），丢弃 agent 归档日志并重试`);
      await discardAgent(sessionId);
      archiveStaleSessionLog(sessionId);
      await runTurn(req, res, sessionId, message);
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
        await serializeTurn(sessionId, () => runTurnHealed(req, res, sessionId, message));
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

  server.listen(residentPort(), "127.0.0.1", () => {
    console.log(`[asa-resident] http://127.0.0.1:${residentPort()}`);
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

  return server;
}

export { apply, inject, name, toolResultSseEvents, chunkSseDelta, createDeltaAggregator, toolCallProgressMessage, TOOL_RESULT_PROGRESS_MESSAGE, archiveStaleSessionLog, encodeSegment, projectKey, isIntermediateAnswer, shouldFollowupCandidateList, CANDIDATE_LIST_FOLLOWUP_PROMPT, mergeCopilotPayload };
export { createSubagentTracker, isSubagentToolCall, subagentSummaryText, subagentTerminalStatus, subagentToolCallLabel } from "./subagent-events.js";
