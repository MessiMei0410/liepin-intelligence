// 轮末对象操作入口聚合：从各工具 tool/result meta.object_refs（asa-tools 投影）
// 收集本轮涉及的业务对象，轮末生成 suggested_actions（前端操作芯片：打开工作流/
// 人选/岗位弹窗）与 references（对象卡）。纯函数模块，不依赖 dsh 包，便于 node --test。

// 操作芯片上限：一轮可能查多个列表，入口只保留前几个主对象（按出现顺序）。
export const SUGGESTED_ACTIONS_MAX = 4;
export const REFERENCES_MAX = 8;
// 同类对象芯片上限：列表查询（asa_jobs/asa_candidates/asa_approvals）一次带出
// 多个同类型对象，全部生成芯片会挤满芯片行且彼此难以区分（2026-08-19 验收：
// 一排 4 个一模一样的“打开岗位”）。同类只保留前 2 个入口，完整列表由
// references 对象卡承载（AgentObjectEmbed 逐个渲染 label+subtitle，不截断）。
export const SAME_TYPE_ACTIONS_MAX = 2;

const ACTION_LABELS = {
  workflow: "查看并审批",
  candidate: "打开人选",
  job: "打开岗位",
};

const REF_FALLBACK_LABELS = {
  workflow: "工作流",
  candidate: "人选",
  job: "岗位",
};

/** 创建一轮的收集器：add(refs) 追加（type+id 去重、保序），done 时取 outputs()。 */
export function createObjectRefCollector() {
  const refs = [];
  const seen = new Set();

  function add(candidates) {
    if (!Array.isArray(candidates)) return;
    for (const ref of candidates) {
      if (!ref || typeof ref !== "object") continue;
      const type = String(ref.type || "");
      if (!Object.hasOwn(ACTION_LABELS, type)) continue;
      if (ref.id === undefined || ref.id === null || String(ref.id).trim() === "") continue;
      const key = `${type}:${String(ref.id)}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const explicitLabel = String(ref.label || "").trim();
      const entry = {
        type,
        id: ref.id,
        label: explicitLabel || REF_FALLBACK_LABELS[type],
      };
      // 操作芯片 label 带对象标题（subtitle 一并下发，前端可展示客户/公司），
      // 避免列表查询后一排“打开岗位”无法区分；对象无显式 label 时退回通用文案。
      entry.action_label = explicitLabel ? `${ACTION_LABELS[type]}：${explicitLabel}` : ACTION_LABELS[type];
      if (typeof ref.subtitle === "string" && ref.subtitle.trim()) entry.subtitle = ref.subtitle;
      if (ref.approval_id) entry.approval_id = ref.approval_id;
      refs.push(entry);
    }
  }

  /** 轮末输出：suggested_actions（≤4，同类 ≤2）+ references（≤8）；均无对象时返回空数组。 */
  function outputs() {
    const perType = new Map();
    const suggested_actions = [];
    for (const ref of refs) {
      if (suggested_actions.length >= SUGGESTED_ACTIONS_MAX) break;
      const count = perType.get(ref.type) || 0;
      if (count >= SAME_TYPE_ACTIONS_MAX) continue;
      perType.set(ref.type, count + 1);
      const action = {
        type: `open_${ref.type}`,
        id: ref.id,
        label: ref.action_label,
      };
      if (ref.subtitle) action.subtitle = ref.subtitle;
      suggested_actions.push(action);
    }
    // references 面向前端对象卡：action_label/approval_id 为 asa-server 内部
    // 附加信息，不下发。
    const references = refs
      .slice(0, REFERENCES_MAX)
      .map(({ approval_id: _approvalId, action_label: _actionLabel, ...ref }) => ref);
    return { suggested_actions, references };
  }

  return { add, outputs };
}
