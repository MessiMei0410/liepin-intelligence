// 轮末对象操作入口聚合：从各工具 tool/result meta.object_refs（asa-tools 投影）
// 收集本轮涉及的业务对象，轮末生成 suggested_actions（前端操作芯片：打开工作流/
// 人选/岗位弹窗）与 references（对象卡）。纯函数模块，不依赖 dsh 包，便于 node --test。

// 操作芯片上限：一轮可能查多个列表，入口只保留前几个主对象（按出现顺序）。
export const SUGGESTED_ACTIONS_MAX = 4;
export const REFERENCES_MAX = 8;

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
      const entry = {
        type,
        id: ref.id,
        label: String(ref.label || "").trim() || REF_FALLBACK_LABELS[type],
      };
      if (typeof ref.subtitle === "string" && ref.subtitle.trim()) entry.subtitle = ref.subtitle;
      if (ref.approval_id) entry.approval_id = ref.approval_id;
      refs.push(entry);
    }
  }

  /** 轮末输出：suggested_actions（≤4）+ references（≤8）；均无对象时返回空数组。 */
  function outputs() {
    const suggested_actions = refs.slice(0, SUGGESTED_ACTIONS_MAX).map((ref) => ({
      type: `open_${ref.type}`,
      id: ref.id,
      label: ACTION_LABELS[ref.type],
    }));
    const references = refs.slice(0, REFERENCES_MAX).map(({ approval_id: _approvalId, ...ref }) => ref);
    return { suggested_actions, references };
  }

  return { add, outputs };
}
