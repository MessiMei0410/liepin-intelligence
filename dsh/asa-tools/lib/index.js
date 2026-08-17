import { defineTool } from "@deepseek-ai/dsh-tools";

// ASA Core 只读工具集（Phase 1）。所有工具只调 GET 只读接口，绝不写库。
// 写动作（preflight/commit/approvals）将在 Phase 2 单独实现并强制走完整安全链路。

const name = "asa-tools";
const inject = ["tools"];

const ASA_BASE = process.env.ASA_CORE_URL || "http://127.0.0.1:8765";
const UA = "ASAApp/dsh-sidecar";

async function getJson(path, exec) {
  const res = await fetch(`${ASA_BASE}${path}`, {
    headers: { Accept: "application/json", "User-Agent": UA },
    signal: exec.signal,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`ASA Core ${path} -> HTTP ${res.status}: ${text.slice(0, 500)}`);
  }
  return res.json();
}

function renderJson(value) {
  const text = JSON.stringify(value, null, 2);
  const MAX = 16000;
  return text.length > MAX ? text.slice(0, MAX) + "\n…(truncated)" : text;
}

function output() {
  return {
    schema: { type: "object", additionalProperties: true },
    render: (_args, value) => [{ type: "text", text: renderJson(value) }],
  };
}

function apply(ctx) {
  ctx.tools.register(defineTool({
    name: "asa_dashboard",
    description:
      "读取 ASA 工作台总览（只读）：活跃岗位数、候选人/待处理计数、待审批数、当前工作流列表。对应 GET /api/v1/dashboard。绝不写库。",
    parameters: {},
    output: output(),
    timeoutMs: 30000,
    isConcurrencySafe: () => true,
    async execute(_args, exec) {
      return await getJson("/api/v1/dashboard", exec);
    },
  }));

  ctx.tools.register(defineTool({
    name: "asa_jobs",
    description:
      "读取 ASA 岗位列表（只读）。返回活跃/在推岗位及其优先级、状态、活跃候选人数。对应 GET /api/v1/jobs。绝不写库。",
    parameters: {},
    output: output(),
    timeoutMs: 30000,
    isConcurrencySafe: () => true,
    async execute(_args, exec) {
      return await getJson("/api/v1/jobs", exec);
    },
  }));

  ctx.tools.register(defineTool({
    name: "asa_candidates",
    description:
      "读取 ASA 候选人列表（只读）。返回候选人及其当前阶段、岗位、公司、职位。对应 GET /api/v1/candidates。绝不写库。",
    parameters: {},
    output: output(),
    timeoutMs: 30000,
    isConcurrencySafe: () => true,
    async execute(_args, exec) {
      return await getJson("/api/v1/candidates", exec);
    },
  }));

  ctx.tools.register(defineTool({
    name: "asa_workflow",
    description:
      "读取单条 ASA 工作流详情（只读）：进度、当前步骤、目标、本轮新增候选人与已评估候选人。对应 GET /api/v1/workflows/{workflow_id}。绝不写库。",
    parameters: {
      workflow_id: {
        type: "string",
        required: true,
        description: "工作流 ID，例如 workflow_ba826dbdccf0。",
      },
    },
    output: output(),
    timeoutMs: 30000,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      return await getJson(`/api/v1/workflows/${encodeURIComponent(args.workflow_id)}`, exec);
    },
  }));

  // ── Phase 2：受控写动作（preflight → commit，带幂等）。安全由 Core 服务端兜底：
  //    一次性 5 分钟 token + Idempotency-Key + 停止/阶段不可倒退校验。
  ctx.tools.register(defineTool({
    name: "asa_candidate_preflight",
    description:
      "对候选人动作做只读预检，返回一次性 preflight token（5 分钟有效）与影响预览，不写库。action 取值：advance=复核通过 / contact=已联系 / recommend=已推荐给客户 / stop=停止推进。",
    parameters: {
      candidate_id: { type: "integer", required: true, description: "job_candidates 关系 ID。" },
      action: { type: "string", required: true, description: "advance | contact | recommend | stop" },
    },
    output: output(),
    timeoutMs: 30000,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const res = await fetch(`${ASA_BASE}/api/v1/candidate-actions/preflight`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json", "User-Agent": UA },
        body: JSON.stringify({ request_id: crypto.randomUUID(), candidate_id: args.candidate_id, action: args.action, note: "", reason: "", preflight_token: "" }),
        signal: exec.signal,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(`preflight -> HTTP ${res.status}: ${JSON.stringify(data).slice(0, 500)}`);
      return data;
    },
  }));

  ctx.tools.register(defineTool({
    name: "asa_candidate_commit",
    description:
      "提交候选人动作（真实写入）：必须携带 asa_candidate_preflight 返回的 token，带 Idempotency-Key 幂等。action 取值同 preflight。绝不在无 token 时提交。",
    parameters: {
      candidate_id: { type: "integer", required: true, description: "job_candidates 关系 ID。" },
      action: { type: "string", required: true, description: "advance | contact | recommend | stop" },
      preflight_token: { type: "string", required: true, description: "asa_candidate_preflight 返回的 token。" },
      note: { type: "string", description: "可选备注。" },
      reason: { type: "string", description: "可选原因。" },
    },
    output: output(),
    timeoutMs: 30000,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      const requestId = crypto.randomUUID();
      const res = await fetch(`${ASA_BASE}/api/v1/candidate-actions/commit`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json", "User-Agent": UA, "Idempotency-Key": requestId },
        body: JSON.stringify({ request_id: requestId, candidate_id: args.candidate_id, action: args.action, note: args.note || "", reason: args.reason || "", preflight_token: args.preflight_token }),
        signal: exec.signal,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(`commit -> HTTP ${res.status}: ${JSON.stringify(data).slice(0, 500)}`);
      return data;
    },
  }));

  // 审批决定（R3 外部寻访等）：decision ∈ {approve, reject, revise}。
  ctx.tools.register(defineTool({
    name: "asa_approval_decision",
    description:
      "对工作流/寻访审批做决定（真实写入）：decision 取值 approve=批准 / reject=拒绝 / revise=退回修改。对应 POST /api/v1/approvals/{approval_id}/decision，带 Idempotency-Key 幂等。",
    parameters: {
      approval_id: { type: "string", required: true, description: "审批 ID，如 approval_xxx。" },
      decision: { type: "string", required: true, description: "approve | reject | revise" },
      note: { type: "string", description: "可选审批备注。" },
    },
    output: output(),
    timeoutMs: 30000,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      const requestId = crypto.randomUUID();
      const res = await fetch(`${ASA_BASE}/api/v1/approvals/${encodeURIComponent(args.approval_id)}/decision`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json", "User-Agent": UA, "Idempotency-Key": requestId },
        body: JSON.stringify({ request_id: requestId, decision: args.decision, note: args.note || "" }),
        signal: exec.signal,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(`approval decision -> HTTP ${res.status}: ${JSON.stringify(data).slice(0, 500)}`);
      return data;
    },
  }));

  // 路 2：委托现有 Python Copilot 回答领域情报问题（只读用途），返回其富答案。
  ctx.tools.register(defineTool({
    name: "asa_copilot_ask",
    description:
      "委托 ASA 现有 Python Copilot 回答需要领域情报的问题（业务上下文、异常分析、优先级、主动建议等），返回其富答案。对应 POST /api/v1/copilot/stream。纯 CRUD 读用 asa_* 工具，写动作走 preflight/commit 或审批工具。",
    parameters: {
      message: { type: "string", required: true, description: "委托给 Copilot 的问题/指令（只读用途）。" },
      context: { type: "object", additionalProperties: true, description: "可选上下文，如 {type:'job', id:137}。" },
    },
    output: output(),
    timeoutMs: 120000,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const requestId = crypto.randomUUID();
      const res = await fetch(`${ASA_BASE}/api/v1/copilot/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream", "User-Agent": UA },
        body: JSON.stringify({ request_id: requestId, session_id: `dsh-${crypto.randomUUID()}`, message: args.message, context: args.context || {} }),
        signal: exec.signal,
      });
      const text = await res.text();
      if (!res.ok) throw new Error(`copilot stream -> HTTP ${res.status}: ${text.slice(0, 500)}`);
      // 解析 SSE：done 事件里的 answer 是最终答案。
      for (const block of text.split(/\r?\n\r?\n/)) {
        let event = "";
        let data = "";
        for (const line of block.split(/\r?\n/)) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (event === "done") {
          try {
            const d = JSON.parse(data);
            return { answer: d.answer || "", references: d.references || [], suggested_actions: d.suggested_actions || [], workflow_id: d.workflow_id ?? null, business_focus: d.business_focus ?? null };
          } catch {
            return { answer: data };
          }
        }
      }
      return { answer: text.slice(-2000) };
    },
  }));
}

export { apply, inject, name };
