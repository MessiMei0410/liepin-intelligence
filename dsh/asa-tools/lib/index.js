import { defineTool } from "@deepseek-ai/dsh-tools";

// ASA Core 工具集。只读工具直调 GET；写动作对模型只暴露「预检申请」：
// asa_candidate_preflight / asa_approval_preflight / asa_workflow_action_preflight
// 只铸造一次性 preflight token（不写库），工具结果经 presentationMeta 投影
// confirm_request → 常驻服务器透传 SSE → 前端确认卡。真正的写入由用户在
// ASA 界面确认后完成（Core 侧 token 需经 UA 门控的 activate 端点激活才可写），
// 模型靠自己的工具面无法完成任何业务写入——人确认是机制，不是 prompt 约定。
//
// UA 约定：必须是「非 ASAApp/ 前缀」。Core 的写确认激活端点
// （POST /api/v1/write-confirmations/activate）按 ASAApp/ UA 前缀门控，
// 本通道的 UA 过不去（此前 "ASAApp/dsh-sidecar" 会被门放过，已修正）。

const name = "asa-tools";
const inject = ["tools"];

const ASA_BASE = process.env.ASA_CORE_URL || "http://127.0.0.1:8765";
const UA = "asa-dsh-tools/1.0";

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

async function postJson(path, payload, exec) {
  const res = await fetch(`${ASA_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json", "User-Agent": UA },
    body: JSON.stringify({ request_id: crypto.randomUUID(), ...payload }),
    signal: exec.signal,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}: ${JSON.stringify(data).slice(0, 500)}`);
  return data;
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

// 业务对象引用投影：只读查询结果里涉及的工作流/候选人/岗位，经 presentationMeta
// 挂到 tool/result 的 meta（完整 JSON 快照，不受 render 16k 截断影响）。常驻服务器
// 轮末据此聚合 suggested_actions/references，让回答里的对象可点击打开（同 action_card 模式）。
// 上限 8 条：列表查询可能很长，轮末操作入口只需要前几个主对象。
const OBJECT_REF_MAX = 8;

function objectRefsMeta(refs) {
  return { object_refs: (Array.isArray(refs) ? refs : []).filter(Boolean).slice(0, OBJECT_REF_MAX) };
}

function listItems(value) {
  return value && typeof value === "object" && Array.isArray(value.items) ? value.items : [];
}

const isRecord = (value) => value && typeof value === "object" && !Array.isArray(value);
const recordOrNull = (value) => (isRecord(value) ? value : null);
const recordList = (value) => (Array.isArray(value) ? value.filter(isRecord) : []);

// 委托会话治理：Copilot 每轮把 user+assistant 落 agent_copilot_messages，会话列表
// 是对该表的 rollup。asa_copilot_ask 此前每次用一次性随机 session（dsh-${uuid}），
// 每调一次就多一个孤儿会话。改为派生自当前 DSH 会话的固定委托 session
// （同一会话内多次委托共享上下文，可审计）；Core 侧 rollup 过滤 ::dsh-delegate
// 后缀与遗留 dsh- 前缀，委托会话不再出现在任务列表。
// 故意不复用 DSH 用户会话本身：委托轮次的 user/assistant 行会和用户轮次交错，
// 恢复会话时消息流错乱，且回填（record-turn）与 copilot() 会写双份 assistant 行。
export function delegateSessionId(exec) {
  const agentId = exec && exec.agent && exec.agent.id != null ? String(exec.agent.id) : "";
  return agentId ? `${agentId}::dsh-delegate` : `dsh-${crypto.randomUUID()}`;
}

// Copilot 脑 done 顶层结构化字段（understanding_card/execution_receipt/工作流进度原料/
// business_focus/model_participation/action_cards/context）投影：常驻服务器据此
// 组装 workflow_progress 并并入轮末 done，前端渲染路径与 Copilot 脑直连一致。
function copilotPayload(value) {
  const d = isRecord(value) ? value : {};
  const actionCard = recordOrNull(d.action_card);
  const actionCards = recordList(d.action_cards);
  return {
    understanding_card: recordOrNull(d.understanding_card),
    execution_receipt: recordOrNull(d.execution_receipt),
    workflow_id: d.workflow_id != null && d.workflow_id !== "" ? String(d.workflow_id) : null,
    workflow: recordOrNull(d.workflow),
    progress: recordOrNull(d.progress),
    plan_summary: recordList(d.plan_summary),
    approvals: recordList(d.approvals),
    goal: recordOrNull(d.goal),
    business_focus: recordOrNull(d.business_focus),
    model_participation: recordOrNull(d.model_participation),
    action_cards: actionCards.length ? actionCards : actionCard ? [actionCard] : [],
    context: recordOrNull(d.context),
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
    output: {
      ...output(),
      presentationMeta: (_args, value) => objectRefsMeta(
        listItems(value).map((item) => item && item.id != null
          ? { type: "job", id: item.id, label: String(item.title || `岗位 #${item.id}`), subtitle: String(item.client || "") }
          : null),
      ),
    },
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
    output: {
      ...output(),
      presentationMeta: (_args, value) => objectRefsMeta(
        listItems(value).map((item) => item && item.id != null
          ? { type: "candidate", id: item.id, label: String(item.name || `人选 #${item.id}`), subtitle: String(item.current_company || "") }
          : null),
      ),
    },
    timeoutMs: 30000,
    isConcurrencySafe: () => true,
    async execute(_args, exec) {
      return await getJson("/api/v1/candidates", exec);
    },
  }));

  // 简历原文/档案体量护栏：full_text 截断，防止一次工具调用撑爆上下文。
  const RESUME_TEXT_CAP = 8000;
  const capText = (value) => {
    const text = String(value || "");
    return text.length > RESUME_TEXT_CAP ? `${text.slice(0, RESUME_TEXT_CAP)}\n…（原文共 ${text.length} 字，已截断，完整原文请在 ASA 界面候选人卡片查看）` : text;
  };

  ctx.tools.register(defineTool({
    name: "asa_candidate_profile",
    description:
      "读取单个候选人完整档案（只读）：基本信息、学历、任职经历、简历原文（summary/work_text/project_text/education_text/full_text，full_text 超 8000 字截断）、最近事件。对应 GET /api/v1/candidates/{candidate_id}。需要简历原文、比对两位人选、核验细节时用本工具，不要凭列表摘要编造简历内容。绝不写库。",
    parameters: {
      candidate_id: {
        type: "number",
        required: true,
        description: "job_candidates 关系 ID（即列表/卡片里的候选人 ID），例如 969。",
      },
    },
    output: {
      ...output(),
      presentationMeta: (_args, value) => {
        const c = value && value.candidate;
        return objectRefsMeta(c && c.id != null
          ? [{ type: "candidate", id: c.id, label: String(c.name || `人选 #${c.id}`), subtitle: String(c.current_company || "") }]
          : []);
      },
    },
    timeoutMs: 30000,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const data = await getJson(`/api/v1/candidates/${Number(args.candidate_id)}`, exec);
      const c = data.candidate || {};
      const resume = c.resume && typeof c.resume === "object" ? c.resume : {};
      return {
        candidate: {
          id: c.id, person_id: c.person_id, name: c.name,
          current_company: c.current_company, current_title: c.current_title,
          city: c.city, education: c.education, experience: c.experience,
          client: c.client, job: c.job, clean_stage: c.clean_stage,
          is_stopped: c.is_stopped, stop_reason_label: c.stop_reason_label,
          resume: {
            summary: resume.summary || "",
            work_text: resume.work_text || "",
            project_text: resume.project_text || "",
            education_text: resume.education_text || "",
            full_text: capText(resume.full_text),
          },
          recent_events: (Array.isArray(c.events) ? c.events : []).slice(-5),
        },
      };
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
    output: {
      ...output(),
      presentationMeta: (args, value) => {
        // id 优先取返回值（value.workflow 是 agent_workflows 行），args 兜底；
        // label 用目标标题，退化为当前阶段。
        const workflow = value && typeof value === "object" && value.workflow && typeof value.workflow === "object" ? value.workflow : {};
        const goal = value && typeof value === "object" && value.goal && typeof value.goal === "object" ? value.goal : {};
        const id = workflow.workflow_id ?? (value && typeof value === "object" ? value.workflow_id : undefined) ?? args.workflow_id;
        return objectRefsMeta(id == null || id === "" ? [] : [{
          type: "workflow", id,
          label: String(goal.title || workflow.current_stage || "工作流"),
        }]);
      },
    },
    timeoutMs: 30000,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      return await getJson(`/api/v1/workflows/${encodeURIComponent(args.workflow_id)}`, exec);
    },
  }));

  ctx.tools.register(defineTool({
    name: "asa_approvals",
    description:
      "读取 ASA 审批列表（只读）：默认只返回待审批（pending）记录，含 approval_id/workflow_id/goal_id/risk_level/title/status/created_at/goal_title。对应 GET /api/v1/approvals。绝不写库。",
    parameters: {
      status: { type: "string", description: "审批状态过滤，默认 pending；传空串表示不按状态过滤。" },
      limit: { type: "integer", description: "返回条数上限，默认 100，最大 500。" },
    },
    output: {
      ...output(),
      presentationMeta: (_args, value) => objectRefsMeta(
        // 审批条目归属于工作流：轮末操作入口是打开对应工作流详情弹窗（查看并审批），
        // approval_id 一并带上供调用方需要时使用。
        listItems(value).map((item) => item && item.workflow_id
          ? {
            type: "workflow", id: item.workflow_id,
            label: String(item.title || item.goal_title || "工作流审批"),
            ...(item.approval_id ? { approval_id: item.approval_id } : {}),
          }
          : null),
      ),
    },
    timeoutMs: 30000,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const params = new URLSearchParams();
      if (args.status !== undefined && args.status !== null) params.set("status", String(args.status));
      if (args.limit) params.set("limit", String(args.limit));
      const qs = params.toString();
      return await getJson(`/api/v1/approvals${qs ? `?${qs}` : ""}`, exec);
    },
  }));

  // 名单生成/筛选（领域能力下沉第一步）：直调 Core 确定性端点，不再委托 Copilot 的
  // LLM 循环出名单。端点虽为 POST，语义纯查询重建——不写库、不建工作流、不走 LLM。
  ctx.tools.register(defineTool({
    name: "asa_pool_filter",
    description:
      "生成/刷新岗位候选人名单（只读）：纯查询重建名单卡，不写库、不建工作流、不走 LLM。对应 POST /api/v1/jobs/{job_id}/candidate-list/refresh。filter_mode 默认 ''（宽松口径：全量名单按阶段分组——未停止/已停止，bonder=true 时固晶/共晶/键合背景单列优先组）；传 'grade_filter' 为严格口径（按岗位职能域硬证据分级 A-核心/A-强/B-中，仅机械/软件/电源类岗位有确定性筛选模型，不支持的岗位会报错）。筛名单/看存量名单一律用本工具，不要委托 asa_copilot_ask 出名单。绝不写库。",
    parameters: {
      job_id: { type: "integer", required: true, description: "岗位 ID（jobs.id），例如 137。" },
      filter_mode: { type: "string", description: "默认 '' 宽松全量名单；'grade_filter' = 严格分级过滤（仅机械/软件/电源域岗位支持）。" },
      bonder: { type: "boolean", description: "true 时固晶/共晶/键合背景候选人单列优先组（仅宽松口径有效），默认 false。" },
    },
    output: {
      ...output(),
      // 名单卡经 presentationMeta 挂到 tool/result 事件的 meta（同 asa_copilot_ask 的
      // action_card 投影）：render 文本有 16k 截断，卡片体量大时不能从 content 反解；
      // 常驻服务器据此透传 SSE card 事件，前端自动弹名单弹窗。object_refs 投影岗位引用。
      presentationMeta: (_args, value) => {
        const card = value && typeof value === "object" && value.card && typeof value.card === "object" ? value.card : null;
        const ctxRef = card && card.context && typeof card.context === "object" ? card.context : null;
        return {
          action_card: card,
          object_refs: ctxRef && ctxRef.type === "job" && ctxRef.id != null
            ? [{ type: "job", id: ctxRef.id, label: String(card.title || `岗位 #${ctxRef.id}`) }]
            : [],
        };
      },
    },
    timeoutMs: 30000,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const jobId = Number(args.job_id);
      if (!Number.isInteger(jobId) || jobId <= 0) {
        throw new Error("asa_pool_filter 要求 job_id 为正整数（jobs.id）。");
      }
      return await postJson(`/api/v1/jobs/${jobId}/candidate-list/refresh`, {
        bonder: args.bonder === true,
        filter_mode: typeof args.filter_mode === "string" ? args.filter_mode.trim() : "",
      }, exec);
    },
  }));

  // ── 写动作 = 预检申请（人确认走 UI 激活，模型面无 commit/decision/action 工具）。
  //    三个 preflight 工具都只读预检 + 铸造一次性 token，绝不写库；presentationMeta
  //    把 confirm_request（token + 动作摘要 + 对象信息）投影到 tool/result meta，
  //    常驻服务器据此透传 SSE confirm_request 事件，前端渲染确认卡。
  //    用户点确认后由前端调 Core activate + 写端点；取消则零写请求。
  const confirmMeta = (build) => ({
    schema: { type: "object", additionalProperties: true },
    render: (_args, value) => [{ type: "text", text: renderJson(value) }],
    presentationMeta: (_args, value) => ({ confirm_request: value && typeof value === "object" ? build(value) : null }),
  });

  ctx.tools.register(defineTool({
    name: "asa_candidate_preflight",
    description:
      "对候选人动作做只读预检并发起写入确认申请（不写库）：返回一次性 preflight token（5 分钟有效）与影响预览；随后 ASA 界面会向用户弹出确认卡，由用户决定是否执行。action 取值：advance=复核通过 / contact=已联系 / recommend=已推荐给客户 / stop=停止推进。回答用户时必须说明「已在界面发起确认，等用户确认后才会写入」，不得声称已完成写入。",
    parameters: {
      candidate_id: { type: "integer", required: true, description: "job_candidates 关系 ID。" },
      action: { type: "string", required: true, description: "advance | contact | recommend | stop" },
    },
    output: confirmMeta((value) => ({
      kind: "candidate_action",
      preflight_token: value.token || "",
      expires_at: value.expires_at || "",
      action: value.action || "",
      candidate: value.candidate && typeof value.candidate === "object" ? value.candidate : {},
      impact: value.impact || "",
    })),
    timeoutMs: 30000,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      return await postJson("/api/v1/candidate-actions/preflight", {
        candidate_id: args.candidate_id, action: args.action, note: "", reason: "", preflight_token: "",
      }, exec);
    },
  }));

  // 审批决定申请（approve=批准 / reject=拒绝 / revise=退回修改）：预检 + 发起界面确认。
  ctx.tools.register(defineTool({
    name: "asa_approval_preflight",
    description:
      "对工作流/寻访审批发起决定确认申请（不写库）：decision 取值 approve=批准 / reject=拒绝 / revise=退回修改。返回一次性 preflight token（5 分钟有效）与审批摘要；随后 ASA 界面会向用户弹出确认卡，由用户决定是否执行。回答用户时必须说明「已在界面发起确认，等用户确认后才会生效」，不得声称已完成审批。",
    parameters: {
      approval_id: { type: "string", required: true, description: "审批 ID，如 approval_xxx（用 asa_approvals 查询）。" },
      decision: { type: "string", required: true, description: "approve | reject | revise" },
      note: { type: "string", description: "可选审批备注。" },
    },
    output: confirmMeta((value) => ({
      kind: "approval_decision",
      preflight_token: value.token || "",
      expires_at: value.expires_at || "",
      approval: value.approval && typeof value.approval === "object" ? value.approval : {},
      note: value.note || "",
      impact: value.impact || "",
    })),
    timeoutMs: 30000,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      return await postJson(`/api/v1/approvals/${encodeURIComponent(args.approval_id)}/decision/preflight`, {
        decision: args.decision, note: args.note || "",
      }, exec);
    },
  }));

  // 工作流动作申请（cancel=关闭 / pause=暂停 / resume=恢复，note 必填）：预检 + 发起界面确认。
  ctx.tools.register(defineTool({
    name: "asa_workflow_action_preflight",
    description:
      "对工作流发起动作确认申请（不写库）：action 取值 cancel=关闭 / pause=暂停 / resume=恢复，note 必填说明原因。返回一次性 preflight token（5 分钟有效）；随后 ASA 界面会向用户弹出确认卡，由用户决定是否执行。回答用户时必须说明「已在界面发起确认，等用户确认后才会生效」，不得声称已执行动作。",
    parameters: {
      workflow_id: { type: "string", required: true, description: "工作流 ID，例如 workflow_a32622d8ff0c。" },
      action: { type: "string", required: true, description: "cancel | pause | resume" },
      note: { type: "string", required: true, description: "必填：执行该动作的原因说明。" },
    },
    output: confirmMeta((value) => ({
      kind: "workflow_action",
      preflight_token: value.token || "",
      expires_at: value.expires_at || "",
      workflow: value.workflow && typeof value.workflow === "object" ? value.workflow : {},
      action: String(value.action || "").replace(/^workflow_action:/, ""),
      note: value.note || "",
      impact: value.impact || "",
    })),
    timeoutMs: 30000,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      if (!String(args.note || "").trim()) {
        throw new Error("asa_workflow_action_preflight 要求 note 必填：请说明执行该动作的原因。");
      }
      return await postJson(`/api/v1/workflows/${encodeURIComponent(args.workflow_id)}/actions/preflight`, {
        action: args.action, note: args.note,
      }, exec);
    },
  }));

  // 路 2：委托现有 Python Copilot 回答领域情报问题（只读用途），返回其富答案。
  ctx.tools.register(defineTool({
    name: "asa_copilot_ask",
    description:
      "委托 ASA 现有 Python Copilot 回答需要领域情报的问题（业务上下文、异常分析、优先级、主动建议等），返回其富答案。对应 POST /api/v1/copilot/stream。纯 CRUD 读用 asa_* 工具，写动作走 asa_*_preflight 申请（人确认在 ASA 界面完成）。",
    parameters: {
      message: { type: "string", required: true, description: "委托给 Copilot 的问题/指令（只读用途）。" },
      context: { type: "object", additionalProperties: true, description: "可选上下文，如 {type:'job', id:137}。" },
    },
    output: {
      ...output(),
      // 名单卡等结构化卡片经 presentationMeta 挂到 tool/result 事件的 meta 上：
      // render 文本有 16k 截断（renderJson），卡片体量大时不能从 content 反解；
      // meta 是完整 JSON 快照，常驻服务器据此向前端透传 SSE card 事件。
      presentationMeta: (_args, value) => ({
        action_card: value && typeof value === "object" && value.action_card && typeof value.action_card === "object" ? value.action_card : null,
        // 其余结构化字段（理解卡/执行回执/工作流进度原料/焦点/模型参与/复数卡片/上下文）
        // 完整 JSON 快照投到 meta.copilot_payload，常驻服务器轮末并入 done。
        copilot_payload: copilotPayload(value),
      }),
    },
    timeoutMs: 120000,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const requestId = crypto.randomUUID();
      const res = await fetch(`${ASA_BASE}/api/v1/copilot/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream", "User-Agent": UA },
        // 委托轮次打标 source=dsh_delegate（落库可审计）；session 复用当前 DSH 会话
        // 派生的固定委托 session，不再每次产生一次性孤儿会话。
        body: JSON.stringify({
          request_id: requestId,
          session_id: delegateSessionId(exec),
          message: args.message,
          context: { ...(args.context || {}), source: "dsh_delegate" },
        }),
        signal: exec.signal,
      });
      const text = await res.text();
      if (!res.ok) throw new Error(`copilot stream -> HTTP ${res.status}: ${text.slice(0, 500)}`);
      // 解析 SSE：done 事件里的 answer 是最终答案；done 原生携带顶层 action_card
      // （/api/v1/copilot/stream 的 done 即 copilot() 完整结果，见 copilot_api.py）。
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
            return {
              answer: d.answer || "",
              references: d.references || [],
              suggested_actions: d.suggested_actions || [],
              action_card: d.action_card && typeof d.action_card === "object" ? d.action_card : null,
              ...copilotPayload(d),
            };
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
