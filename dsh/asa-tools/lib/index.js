import { defineTool } from "@deepseek-ai/dsh-tools";

// ASA Core 工具集。只读工具直调 GET；写动作对模型只暴露「预检申请」：
// asa_candidate_preflight / asa_approval_preflight / asa_workflow_action_preflight
// / asa_resume_backfill / asa_job_filter_note_preflight
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
    ...(recordOrNull(d.analysis_card) ? { analysis_card: recordOrNull(d.analysis_card) } : {}),
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

// asa_copilot_ask 超时与流式读取（dogfood P1-1：名单/策略类委托富答案生成 2-4 分钟，
// 旧的 120s 绝对超时 + 整流缓冲导致连续 3 次超时、300s 轮预算被 abort）：
// - timeoutMs 上调到 280s（ASA_DSH_TURN_TIMEOUT_MS 300s 轮预算内最多一次完整尝试，
//   不再 3×120s 白烧轮预算）；
// - 增量读 SSE：完整的 done 块到达即返回，不等连接收尾；
// - 静默看门狗：慢但在产出（progress/thinking 持续来）不算超时，超过
//   INACTIVITY_MS 没有任何字节才判卡死。
const COPILOT_ASK_TIMEOUT_MS = Number(process.env.ASA_COPILOT_ASK_TIMEOUT_MS || 280_000);
const COPILOT_ASK_INACTIVITY_MS = Number(process.env.ASA_COPILOT_ASK_INACTIVITY_MS || 90_000);

function hasCompleteDoneBlock(buffer) {
  // done 块完整性的两种判据：块后有终止空行（不是流尾残片），或 data 已是合法 JSON
  //（done 多为最后一个写块，服务器可能不收尾，靠 JSON 可解析判完整，避免截断提前返回）。
  const blocks = buffer.split(/\r?\n\r?\n/);
  for (let index = 0; index < blocks.length; index += 1) {
    const block = blocks[index];
    if (!block || !/(?:^|\r?\n)event:\s*done(?:\r?\n|$)/.test(block)) continue;
    if (index < blocks.length - 1) return true; // 后面还有内容，本块必然完整
    const dataLine = block.split(/\r?\n/).filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trim()).join("");
    if (!dataLine) continue;
    try {
      JSON.parse(dataLine);
      return true;
    } catch { /* data 还在路上，继续等 */ }
  }
  return false;
}

async function readCopilotSse(res) {
  // 无流式 body 的环境（测试桩/旧运行时）退回整体文本。
  if (!res.body || typeof res.body.getReader !== "function") return await res.text();
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      let watchdog;
      const inactivity = new Promise((_, reject) => {
        watchdog = setTimeout(
          () => reject(new Error(`copilot stream 静默超过 ${Math.round(COPILOT_ASK_INACTIVITY_MS / 1000)}s，判定卡死`)),
          COPILOT_ASK_INACTIVITY_MS,
        );
      });
      let chunk;
      try {
        chunk = await Promise.race([reader.read(), inactivity]);
      } finally {
        clearTimeout(watchdog);
      }
      const { value, done } = chunk;
      buffer += decoder.decode(value, { stream: !done });
      if (done) return buffer;
      if (hasCompleteDoneBlock(buffer)) return buffer; // done 提前返回
    }
  } finally {
    try { await reader.cancel(); } catch { /* 连接已收尾时 cancel 报错可忽略 */ }
  }
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
        // label 优先用 goal 标题（含客户/岗位/轮次，如「长越科技 / 机械高级工程师｜第5轮寻访」）：
        // 同名 R3 审批（审批 title 都是"执行多渠道寻访"）的芯片由此可区分
        // （2026-08-19 dogfood：士兰微/长越两条芯片文案完全相同）。原审批 title 降为
        // 命中别名：回答引用"执行多渠道寻访"时相关性过滤（#71/#78）仍判命中。
        listItems(value).map((item) => item && item.workflow_id
          ? {
            type: "workflow", id: item.workflow_id,
            label: String(item.goal_title || item.title || "工作流审批"),
            // 仅当 label 确实换成了 goal 标题才把审批 title 降为别名（否则别名与 label 相同，冗余）。
            ...(item.goal_title && item.title && String(item.title) !== String(item.goal_title) ? { aliases: [String(item.title)] } : {}),
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
      "生成/刷新岗位候选人名单（只读）：纯查询重建名单卡，不写库、不建工作流、不走 LLM。对应 POST /api/v1/jobs/{job_id}/candidate-list/refresh。filter_mode 默认 ''（宽松口径：全量名单按阶段分组——未停止/已停止，bonder=true 时固晶/共晶/键合背景单列优先组）；传 'grade_filter' 为严格口径（按岗位职能域硬证据分级 A-核心/A-强/B-中，仅机械/软件/电源类岗位有确定性筛选模型，不支持的岗位会报错）。岗位有筛选口径便签时，返回的 answer/卡片会带「口径便签」声明——回答用户时必须照该口径声明说明生效的口径。名单卡只是证据输入；用户给出优先/匹配/经验要求等筛选条件时，调用后必须继续证据核验并给出已确认、相邻经验、待核验/不满足的分档判断、推荐顺序、依据和下一步，不能只返回名单。筛名单/看存量名单一律用本工具，不要委托 asa_copilot_ask 出名单。绝不写库。",
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

  // 疑似重复人选扫描（护栏第 6 条配套，只读）：Core 按「姓氏+公司+职位」三证据
  // 口径聚类 job_candidates，返回组内关系数 >1 的疑似重复组；每组带建议保留方
  // （suggested_winner_id）。合并动作走 asa_candidate_preflight(action=merge)。
  ctx.tools.register(defineTool({
    name: "asa_dedupe_scan",
    description:
      "扫描候选人池疑似重复关系（只读）：按「姓氏+当前公司+当前职位」三证据口径聚类，返回疑似重复组（含各关系 id/姓名/阶段/停止状态/来源/最近事件时间/person_id 与建议保留方 suggested_winner_id）。对应 GET /api/v1/candidates/dedupe-scan。绝不写库；确认重复后如需合并，用 asa_candidate_preflight(action=merge, winner_id+loser_id) 发起界面确认。",
    parameters: {
      job_id: { type: "integer", description: "可选：只扫描某个岗位的候选人关系。" },
    },
    output: {
      ...output(),
      presentationMeta: (_args, value) => objectRefsMeta(
        (value && typeof value === "object" && Array.isArray(value.groups) ? value.groups : [])
          .flatMap((group) => (Array.isArray(group.members) ? group.members : []))
          .map((member) => member && member.relation_id != null
            ? {
              type: "candidate", id: member.relation_id,
              label: String(member.name || `人选 #${member.relation_id}`),
              subtitle: String(member.current_company || ""),
            }
            : null),
      ),
    },
    timeoutMs: 30000,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const params = new URLSearchParams();
      if (args.job_id) params.set("job_id", String(args.job_id));
      const qs = params.toString();
      return await getJson(`/api/v1/candidates/dedupe-scan${qs ? `?${qs}` : ""}`, exec);
    },
  }));

  // 岗位筛选口径便签（dogfood R2-3）：跨会话"以后筛选按 X 口径"的持久化通道。
  // 便签是给人和模型看的口径声明（asa_pool_filter 出名单卡时随口径声明显示），
  // 不参与确定性筛选逻辑；写走确认链（本工具只读，写用 asa_job_filter_note_preflight）。
  ctx.tools.register(defineTool({
    name: "asa_job_filter_notes",
    description:
      "读取岗位筛选口径便签（只读）：返回该岗位当前生效的口径便签（note/updated_at，无便签时 note=null）。对应 GET /api/v1/jobs/{job_id}/filter-notes。回答「这个岗位筛选口径是什么/之前记的偏好生效了吗」时用本工具查证，不要凭会话记忆声称。绝不写库。",
    parameters: {
      job_id: { type: "integer", required: true, description: "岗位 ID（jobs.id），例如 137。" },
    },
    output: output(),
    timeoutMs: 30000,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const jobId = Number(args.job_id);
      if (!Number.isInteger(jobId) || jobId <= 0) {
        throw new Error("asa_job_filter_notes 要求 job_id 为正整数（jobs.id）。");
      }
      return await getJson(`/api/v1/jobs/${jobId}/filter-notes`, exec);
    },
  }));

  // 口径便签写申请工具在 confirmMeta 定义之后注册（见下）。

  // 子集名单卡（与 asa_pool_filter 互补）：精读/评审/去重等"指定一组候选人"场景
  // 出可操作 candidate_list 卡（用户规矩：凡给名单必须给可操作名单卡，禁止纯
  // markdown 表格名单）。端点语义同 refresh——纯查询组装，不写库不走 LLM。
  ctx.tools.register(defineTool({
    name: "asa_candidate_list_card",
    description:
      "把一组指定候选人（job_candidates id）输出为可操作名单卡（只读）：精读/评审/去重等场景产出子集名单（如「精读 20 人后 ✅ 通过 4 人」）时必须用本工具出卡，禁止只给 markdown 表格名单。对应 POST /api/v1/candidates/list-card，纯查询组装——不写库、不建工作流、不走 LLM；库中不存在的 id 会在卡片 summary.skipped 注明，不报错。整池筛选/全量名单用 asa_pool_filter，指定子集名单用本工具。",
    parameters: {
      candidate_ids: { type: "array", required: true, description: "job_candidates 关系 ID 数组（正整数），数组顺序即卡片内顺序。" },
      title: { type: "string", required: true, description: "名单标题，如「长越机械｜精读通过名单」。" },
      groups: { type: "array", description: "可选分组：[{key, label, candidate_ids, priority?}]，如 [{key:'passed',label:'✅ 通过',candidate_ids:[...]}]；未被任何组覆盖的 id 自动归入「未分组」。" },
      job_id: { type: "integer", description: "可选岗位上下文（jobs.id）；名单属于某岗位时传入，卡片可跳转岗位详情并投影岗位引用。跨岗位子集（如去重扫描结果）不传。" },
    },
    output: {
      ...output(),
      // 同 asa_pool_filter 的投影链路：card → meta.action_card → SSE card → 前端名单弹窗；
      // context 为岗位时同时投影 object_refs 岗位引用。
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
      const rawIds = Array.isArray(args.candidate_ids) ? args.candidate_ids : [];
      if (!rawIds.length) {
        throw new Error("asa_candidate_list_card 要求 candidate_ids 为非空数组（job_candidates.id）。");
      }
      const ids = rawIds.map(Number);
      if (ids.some((id) => !Number.isInteger(id) || id <= 0)) {
        throw new Error("asa_candidate_list_card 的 candidate_ids 必须全部是正整数（job_candidates.id）。");
      }
      const title = typeof args.title === "string" ? args.title.trim() : "";
      if (!title) {
        throw new Error("asa_candidate_list_card 要求 title 非空。");
      }
      const body = { candidate_ids: ids, title };
      if (Array.isArray(args.groups) && args.groups.length) {
        body.groups = args.groups.map((group, index) => {
          const g = group && typeof group === "object" ? group : {};
          const gids = (Array.isArray(g.candidate_ids) ? g.candidate_ids : []).map(Number);
          if (gids.some((id) => !Number.isInteger(id) || id <= 0)) {
            throw new Error(`groups[${index}].candidate_ids 必须全部是正整数。`);
          }
          return {
            key: String(g.key || `group${index + 1}`),
            label: String(g.label || g.key || `分组 ${index + 1}`),
            candidate_ids: gids,
            priority: g.priority === true,
          };
        });
      }
      if (args.job_id !== undefined && args.job_id !== null) {
        const jobId = Number(args.job_id);
        if (!Number.isInteger(jobId) || jobId <= 0) {
          throw new Error("asa_candidate_list_card 的 job_id 为正整数（jobs.id）。");
        }
        body.context = { type: "job", id: jobId };
      }
      return await postJson("/api/v1/candidates/list-card", body, exec);
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
      "对候选人动作做只读预检并发起写入确认申请（不写库）：返回一次性 preflight token（5 分钟有效）与影响预览；随后 ASA 界面会向用户弹出确认卡，由用户决定是否执行。action 取值：advance=复核通过 / contact=已联系 / recommend=已推荐给客户 / stop=停止推进 / merge=合并去重（废弃方停止并指向保留方；需三证据：姓氏+公司+职位同时匹配，先用 asa_dedupe_scan 只读扫描确认疑似重复组）/ record_event=记录生命周期事件（面试/Offer/入职：event_type 六枚举 interview_scheduled/interview_completed/offer_extended/offer_accepted/offer_declined/onboarded，occurred_at 事件时间如 2026-08-20 14:00，event_status 选填，note 备注）。merge 必须携带 winner_id（保留方关系 id，缺省取 candidate_id）与 loser_id（废弃方关系 id）。记录面试安排一律用 record_event，不要拿 contact 凑。回答用户时必须说明「已在界面发起确认，等用户确认后才会写入」，不得声称已完成写入。",
    parameters: {
      candidate_id: { type: "integer", required: true, description: "job_candidates 关系 ID（merge 时为保留方，同 winner_id）。" },
      action: { type: "string", required: true, description: "advance | contact | recommend | stop | merge | record_event" },
      winner_id: { type: "integer", description: "merge 必填：保留方关系 ID（缺省取 candidate_id）。" },
      loser_id: { type: "integer", description: "merge 必填：废弃方关系 ID（该关系将被停止并指向保留方）。" },
      event_type: { type: "string", description: "record_event 必填：interview_scheduled | interview_completed | offer_extended | offer_accepted | offer_declined | onboarded。" },
      occurred_at: { type: "string", description: "record_event 选填：事件时间（如 2026-08-20 14:00），缺省为服务端当前时间。" },
      event_status: { type: "string", description: "record_event 选填：事件状态（缺省用事件类型默认状态）。" },
      note: { type: "string", description: "record_event 选填：事件备注（如「一面，客户现场」）。" },
    },
    output: confirmMeta((value) => ({
      kind: "candidate_action",
      preflight_token: value.token || "",
      expires_at: value.expires_at || "",
      action: value.action || "",
      candidate: value.candidate && typeof value.candidate === "object" ? value.candidate : {},
      impact: value.impact || "",
      // record_event：确认卡展示事件要点（类型/时间/状态/备注）。
      ...(value.action === "record_event" && value.event && typeof value.event === "object"
        ? { event: value.event }
        : {}),
      // merge：确认卡展示双方关键字段 diff（姓名/公司/职位/阶段/来源/person_id/简历摘要）。
      ...(value.action === "merge" && value.loser && typeof value.loser === "object"
        ? {
          merge: {
            winner: value.winner && typeof value.winner === "object" ? value.winner : {},
            loser: value.loser,
            diff: Array.isArray(value.diff) ? value.diff : [],
            loser_already_stopped: Boolean(value.loser_already_stopped),
          },
        }
        : {}),
    })),
    timeoutMs: 30000,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const payload = {
        candidate_id: args.candidate_id, action: args.action, note: "", reason: "", preflight_token: "",
      };
      if (args.action === "merge") {
        const winnerId = args.winner_id ?? args.candidate_id;
        if (!Number.isInteger(winnerId) || !Number.isInteger(args.loser_id)) {
          throw new Error("asa_candidate_preflight(action=merge) 要求 winner_id 与 loser_id 均为关系 ID（整数）：先用 asa_dedupe_scan 扫描确认疑似重复组。");
        }
        payload.candidate_id = winnerId;
        payload.loser_id = args.loser_id;
      }
      if (args.action === "record_event") {
        if (!String(args.event_type || "").trim()) {
          throw new Error("asa_candidate_preflight(action=record_event) 要求 event_type（interview_scheduled/interview_completed/offer_extended/offer_accepted/offer_declined/onboarded）。");
        }
        payload.event_type = String(args.event_type).trim();
        if (args.occurred_at) payload.occurred_at = String(args.occurred_at);
        if (args.event_status) payload.event_status = String(args.event_status);
        payload.note = String(args.note || "");
      }
      return await postJson("/api/v1/candidate-actions/preflight", payload, exec);
    },
  }));

  // 口径便签写申请（不写库）：只铸一次性 preflight token，confirm_request 经
  // presentationMeta 投影 → 前端确认卡；用户确认后由前端调 Core 激活 + 写端点。
  // 未落库前模型不得声称"已记录"（护栏第 18 条）。
  ctx.tools.register(defineTool({
    name: "asa_job_filter_note_preflight",
    description:
      "申请保存岗位筛选口径便签（不写库）：用户要求「以后筛选都按某口径/偏好」（如「六自由度运动台作为大加分项」）时用本工具发起界面确认。返回一次性 preflight token（5 分钟有效）并与当前便签对照；随后 ASA 界面弹出确认卡，由用户决定是否保存。便签是口径声明：保存后出名单卡时随口径声明显示，不改变确定性筛选逻辑本身（关键词变更只能走代码变更，不得承诺「已改筛选逻辑」）。回答用户时必须说明「已在界面发起确认，等用户确认后才会保存」，未确认前不得声称「已记录/已记住」。",
    parameters: {
      job_id: { type: "integer", required: true, description: "岗位 ID（jobs.id），例如 137。" },
      note: { type: "string", required: true, description: "口径便签内容（≤500 字），如「六自由度运动台（6-DOF）经验作为大加分项」。" },
    },
    output: confirmMeta((value) => ({
      kind: "filter_note",
      preflight_token: value.token || "",
      expires_at: value.expires_at || "",
      action: "job_filter_note",
      job: value.job && typeof value.job === "object" ? value.job : {},
      note: value.note || "",
      previous_note: value.previous_note || "",
      impact: value.impact || "",
    })),
    timeoutMs: 30000,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const jobId = Number(args.job_id);
      if (!Number.isInteger(jobId) || jobId <= 0) {
        throw new Error("asa_job_filter_note_preflight 要求 job_id 为正整数（jobs.id）。");
      }
      const note = typeof args.note === "string" ? args.note.trim() : "";
      if (!note) {
        throw new Error("asa_job_filter_note_preflight 要求 note 非空。");
      }
      return await postJson(`/api/v1/jobs/${jobId}/filter-notes/preflight`, { note }, exec);
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

  // 简历回填申请：用户说"我打开了某人的详情页，更新他的简历"时使用。预检在 Core
  // 侧完成：从桥接存储读当前页简历快照（扩展直推，模型不接触全文）、定位本地
  // 候选人（外部 ID 只是证据，匹配不到即 409 不新建）、完整性守卫（partial 抓取
  // 409）、新旧 diff；通过则发一次性未激活 token，界面确认卡由用户决定写入。
  ctx.tools.register(defineTool({
    name: "asa_resume_backfill",
    description:
      "对当前打开的猎聘详情页发起简历回填确认申请（不写库）：Core 读取页面简历快照（扩展直推），定位本地候选人（candidate_id 或猎聘 resume_id 至少给其一；匹配不到本地档案会 409，绝不新建记录），校验抓取完整性（partial 快照 409），生成新旧简历 diff 并返回一次性 preflight token（5 分钟有效）；随后 ASA 界面会向用户弹出确认卡，由用户决定是否写入。简历无变化时返回 unchanged=true（不发 token，直接告知用户已是最新）。回答用户时必须说明「已在界面发起确认，等用户确认后才会写入」，不得声称已完成写入。",
    parameters: {
      candidate_id: { type: "integer", description: "job_candidates 关系 ID；已知时优先传（与当前页快照身份不一致会 409）。" },
      resume_id: { type: "string", description: "猎聘外部档案 ID（res_id_encode）；不知道关系 ID 时用它反查本地人选。" },
    },
    output: confirmMeta((value) => {
      // 无变化（unchanged）：不发确认卡，模型直接告知用户档案已是最新。
      if (value.unchanged) return null;
      return {
        kind: "resume_backfill",
        preflight_token: value.token || "",
        expires_at: value.expires_at || "",
        action: "resume_backfill",
        candidate: value.candidate && typeof value.candidate === "object" ? value.candidate : {},
        resume: value.resume && typeof value.resume === "object" ? value.resume : {},
        // 新旧 diff：确认卡展示哪些段新增/变化（字数与摘要）。
        diff: Array.isArray(value.diff) ? value.diff : [],
        impact: value.impact || "",
      };
    }),
    timeoutMs: 30000,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const candidateId = Number(args.candidate_id || 0);
      const resumeId = typeof args.resume_id === "string" ? args.resume_id.trim() : "";
      if (!candidateId && !resumeId) {
        throw new Error("asa_resume_backfill 要求 candidate_id 或 resume_id 至少提供其一。");
      }
      const payload = {};
      if (candidateId) payload.candidate_id = candidateId;
      if (resumeId) payload.resume_id = resumeId;
      return await postJson("/api/v1/candidates/resume-backfill/preflight", payload, exec);
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
    timeoutMs: COPILOT_ASK_TIMEOUT_MS,
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
      if (!res.ok) throw new Error(`copilot stream -> HTTP ${res.status}: ${(await res.text()).slice(0, 500)}`);
      // 增量读流：done 块完整到达即返回（富答案生成 2-4 分钟属正常，整流缓冲 +
      // 120s 绝对超时是 dogfood P1-1 连续超时的直接原因）；静默看门狗判真卡死。
      const text = await readCopilotSse(res);
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
