import { randomUUID } from "node:crypto";
import http from "node:http";
import { installModelSelection } from "@deepseek-ai/dsh-agent";
import { createUserMessage } from "@deepseek-ai/dsh-llm";
import { SessionId } from "@deepseek-ai/dsh-session";

/**
 * @asa/dsh-asa-server — ASA 常驻 Agent 服务器（resident runner）。
 * 与 dsh-headless 的 one-shot runner 同源：都用 agents/sessions/agentDefaultModel
 * 跑 agent 循环，但不 exit，而是起一个 node:http 服务器：
 *   POST /turn {message, session_id?} -> {ok, session_id, answer}
 *   GET  /health                     -> {ok}
 * 会话复用：同一 session_id 复用同一个 live agent，实现多轮记忆与摊销启动成本。
 */

const name = "asa-resident-runner";
const inject = ["agentDefaultModel", "agents", "sessions"];

const PORT = Number(process.env.ASA_DSH_RESIDENT_PORT || 8891);

/** 把本轮新产生的事件聚合为最终文本 + 结束原因（与 headless 的 summarize 同构）。 */
function summarize(events, firstSeq) {
  let started = false;
  let text = "";
  let reason;
  for (const event of events) {
    if (event.seq < firstSeq) continue;
    if (event.type === "turn/start") {
      started = true;
      continue;
    }
    if (!started) continue;
    if (event.type === "assistant/message") {
      const joined = event.data.message.content
        .filter((block) => block.type === "text")
        .map((block) => block.text)
        .join("");
      if (joined !== "") text = joined;
    }
    if (event.type === "turn/end") reason = event.data.reason;
  }
  return { text, reason };
}

/** 取 live agent，没有则按默认模型新建（agent.id === session.id，故按 sessionId 可查回）。 */
async function ensureAgent(ctx, sessionId) {
  const agents = ctx.get("agents");
  const existing = agents.get(sessionId);
  if (existing) return existing;
  const defaultModel = ctx.get("agentDefaultModel");
  const selection = defaultModel.currentSelection();
  const { agent } = await agents.create({
    sessionId: SessionId(sessionId),
    meta: { cwd: process.cwd() },
    agentOptions: { provider: selection.provider, model: selection.model },
    setup: (agentCtx) => {
      installModelSelection(agentCtx, { current: selection, assembled: void 0 });
    },
  });
  await agent.whenIdle();
  return agent;
}

async function readBody(req) {
  let body = "";
  for await (const chunk of req) body += chunk;
  return body;
}

function apply(ctx) {
  const headers = {
    "Content-Type": "application/json; charset=utf-8",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };

  const server = http.createServer(async (req, res) => {
    if (req.method === "OPTIONS") {
      res.writeHead(204, headers);
      res.end();
      return;
    }
    if (req.method === "GET" && req.url === "/health") {
      res.writeHead(200, headers);
      res.end(JSON.stringify({ ok: true, profile: "asa-server" }));
      return;
    }
    if (req.method === "POST" && req.url === "/turn") {
      let payload = {};
      try {
        payload = JSON.parse((await readBody(req)) || "{}");
      } catch {
        payload = {};
      }
      const message = String(payload.message || "").trim();
      const sessionId = String(payload.session_id || `asa-${randomUUID()}`);
      if (!message) {
        res.writeHead(400, headers);
        res.end(JSON.stringify({ ok: false, error: "message is required" }));
        return;
      }
      try {
        const agent = await ensureAgent(ctx, sessionId);
        const firstSeq = agent.session.seq;
        agent.followup(
          createUserMessage({
            content: [{ type: "text", text: message }],
            source: { kind: "user" },
          }),
        );
        await agent.whenIdle();
        const outcome = summarize(agent.session.events, firstSeq);
        res.writeHead(200, headers);
        res.end(
          JSON.stringify({
            ok: outcome.reason?.kind === "completed",
            session_id: sessionId,
            answer: outcome.text,
            error: outcome.reason?.kind === "error" ? outcome.reason.error.message : void 0,
          }),
        );
      } catch (error) {
        res.writeHead(500, headers);
        res.end(JSON.stringify({ ok: false, error: error instanceof Error ? error.message : String(error) }));
      }
      return;
    }
    res.writeHead(404, headers);
    res.end(JSON.stringify({ ok: false, error: "not found" }));
  });

  server.listen(PORT, "127.0.0.1", () => {
    console.log(`[asa-resident] http://127.0.0.1:${PORT}`);
  });
}

export { apply, inject, name };
