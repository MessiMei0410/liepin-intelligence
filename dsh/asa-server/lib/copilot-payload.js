// asa_copilot_ask 委托载荷 → 轮末 done 字段：presentationMeta 把 Copilot 脑 done 的
// 结构化字段投到 tool/result meta.copilot_payload（完整 JSON 快照），这里组装成
// 前端 Copilot 脑直连同形的 done 字段——理解卡/执行回执/焦点条/模型参与 badge/
// 复数卡片/上下文原样透传，工作流进度原料（workflow_id/workflow/progress/
// plan_summary/approvals/goal）按 Core bridge（service_copilot_bridge.py）的结构
// 组装成 workflow_progress。纯函数模块，不依赖 dsh 包，便于 node --test。

const isRecord = (value) => value && typeof value === "object" && !Array.isArray(value);

/** workflow 进度原料 → workflow_progress（与 service_copilot_bridge.py:393-401 同形）。 */
export function buildWorkflowProgress(payload) {
  if (!isRecord(payload)) return null;
  const workflowId = String(payload.workflow_id || "").trim();
  if (!workflowId) return null;
  const workflow = isRecord(payload.workflow) ? payload.workflow : {};
  const progress = isRecord(payload.progress) ? payload.progress : {};
  const goal = isRecord(payload.goal) ? payload.goal : {};
  const planSummary = Array.isArray(payload.plan_summary) ? payload.plan_summary : [];
  const completed = Number(progress.completed || 0);
  const total = Number(progress.total || 0) || planSummary.length;
  return {
    workflow_id: workflowId,
    status: workflow.status || "queued",
    business_outcome: workflow.business_outcome || goal.business_outcome || null,
    completed: Number.isFinite(completed) ? completed : 0,
    total: Number.isFinite(total) ? total : 0,
    label: workflow.current_stage || "准备执行",
    pending_approvals: Array.isArray(payload.approvals) ? payload.approvals.filter(isRecord) : [],
  };
}

/** meta.copilot_payload → 并入 done 的字段（只携带实际存在的键）。 */
export function delegateDoneFields(payload) {
  if (!isRecord(payload)) return {};
  const fields = {};
  if (isRecord(payload.understanding_card)) fields.understanding_card = payload.understanding_card;
  if (isRecord(payload.execution_receipt)) fields.execution_receipt = payload.execution_receipt;
  if (isRecord(payload.business_focus)) fields.business_focus = payload.business_focus;
  if (isRecord(payload.model_participation)) fields.model_participation = payload.model_participation;
  if (isRecord(payload.context)) fields.context = payload.context;
  if (Array.isArray(payload.action_cards) && payload.action_cards.length) fields.action_cards = payload.action_cards;
  const workflowProgress = buildWorkflowProgress(payload);
  if (workflowProgress) {
    fields.workflow_id = workflowProgress.workflow_id;
    fields.workflow_progress = workflowProgress;
  }
  return fields;
}
