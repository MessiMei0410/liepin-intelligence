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
        // 有真实对象名（非兜底文案）才能按 answer 文本命中过滤（见 outputs）。
        named: Boolean(explicitLabel),
        // 命中别名：label 换成目标/岗位标题后（如审批芯片用 goal_title 区分同名审批），
        // 回答引用原动作标题（"执行多渠道寻访"）时仍算命中，相关性过滤（#71/#78）不误杀。
        aliases: Array.isArray(ref.aliases)
          ? ref.aliases.map(value => String(value || "").trim()).filter(value => value && value !== (explicitLabel || REF_FALLBACK_LABELS[type]))
          : [],
      };
      // 操作芯片 label 带对象标题（subtitle 一并下发，前端可展示客户/公司），
      // 避免列表查询后一排“打开岗位”无法区分；对象无显式 label 时退回通用文案。
      entry.action_label = explicitLabel ? `${ACTION_LABELS[type]}：${explicitLabel}` : ACTION_LABELS[type];
      if (typeof ref.subtitle === "string" && ref.subtitle.trim()) entry.subtitle = ref.subtitle;
      if (ref.approval_id) entry.approval_id = ref.approval_id;
      refs.push(entry);
    }
  }

  /**
   * 轮末输出：suggested_actions（≤4，同类 ≤2）+ references（≤8）；均无对象时返回空数组。
   * options.candidateListCard：本轮已投影 candidate_list action_card 时置真——名单卡
   *   本身提供全部人选入口，candidate 的 references 与 open_candidate 芯片全部抑制
   *   （2026-08-19 验收：名单回答下再嵌 8 张候选人对象卡全是噪音）。
   * options.answer：本轮最终回答文本。有显式名称的引用（named）只保留 label 或
   *   subtitle（客户/公司）在回答中实际命中的——工具原始结果里的对象不等于回答
   *   提及的对象（2026-08-19 验收：长越名单下出现"打开岗位：电源专家"）。无名引用
   *   （兜底文案）job/workflow 保底保留，candidate 丢弃。references 与 suggested_actions
   *   共用同一相关性判定，芯片不再绕过过滤。
   */
  function outputs(options = {}) {
    const answer = String(options.answer || "");
    // 强命中 = label 或别名出现在回答里；弱命中 = 客户/公司名（subtitle）命中。
    // subtitle 匹配先剥公司后缀——回答里说「长越/长川」而 subtitle 是「长越科技」
    // （2026-08-20 实证：全名匹配会漏、退化成全量兜底）。
    const shortClientForms = (subtitle) => {
      const base = String(subtitle || "").trim();
      if (!base) return [];
      const short = base.replace(/（.*?）/g, "").replace(/(科技|股份|集团|控股|微电子|半导体|技术)?(股份|集团|控股)?(有限公司|有限责任公司|股份有限公司)?$/, "").trim();
      return short && short !== base ? [base, short] : [base];
    };
    const strongHit = (ref) => answer.includes(ref.label) || ref.aliases.some((alias) => answer.includes(alias));
    const weakHit = (ref) => shortClientForms(ref.subtitle).some((form) => form.length >= 2 && answer.includes(form));
    const relevant = (ref) => {
      if (!answer) return true; // 无回答文本不过滤（兼容无 answer 调用方）
      if (ref.type === "candidate") return ref.named && strongHit(ref);
      if (!ref.named) return true; // 无名 job/workflow 保底
      return strongHit(ref);
    };
    const visibleAll = options.candidateListCard ? refs.filter((ref) => ref.type !== "candidate") : refs;
    let visible = visibleAll.filter(relevant);
    if (answer && visible.length === 0) {
      // 分级兜底（2026-08-20 验收：「长越 4 岗+长川 6 岗」式回答岗位名零命中时，
      // 全量兜底把士兰微的无关岗位也放出来了）：先按弱命中（客户名）收窄——
      // 同客户多岗位已有强命中在上一轮挡住，跨客户误留由弱命中过滤；
      // 弱命中也为零才整组放回 job/workflow。candidate 一律不参与兜底。
      const weak = visibleAll.filter((ref) => ref.type !== "candidate" && ref.named && weakHit(ref));
      visible = weak.length > 0 ? weak : visibleAll.filter((ref) => ref.type !== "candidate");
    }
    const perType = new Map();
    const suggested_actions = [];
    for (const ref of visible) {
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
    // references 面向前端对象卡：action_label/approval_id/named/aliases 为 asa-server 内部
    // 附加信息，不下发。
    const references = visible
      .slice(0, REFERENCES_MAX)
      .map(({ approval_id: _approvalId, action_label: _actionLabel, named: _named, aliases: _aliases, ...ref }) => ref);
    return { suggested_actions, references };
  }

  return { add, outputs };
}
