import type { CandidateListCardData } from '../workflows/CandidateListCard'

// 名单卡内容签名：识别"新卡"与"同一张卡的重复投影"。DSH 委托轮会把上一轮已投影的
// candidate_list 卡并入后续轮次的 done（mergeCopilotPayload 卡片携带语义），同一张卡
// 随每条新回复重复到达——2026-08-19 dogfood：名单弹窗逐条回复重复弹出遮挡正文。
// 签名覆盖标题/上下文/口径/汇总计数/分组与人选 id 列表：内容真实变化（刷新、新筛选）
// 视为新卡，原样重复投影视为同卡。
export const candidateListCardSignature = (card: CandidateListCardData): string => {
  const summary = card.summary || {}
  const groups = (Array.isArray(card.groups) ? card.groups : []).map(group => [
    group.key || '',
    group.label || '',
    (Array.isArray(group.candidates) ? group.candidates : []).map(candidate => candidate.id).join(','),
  ].join(':'))
  return JSON.stringify({
    title: card.title || '',
    context: card.context ? `${card.context.type}:${card.context.id}` : '',
    filter: card.filter_mode || '',
    subset: card.subset === true,
    total: summary.total ?? null,
    active: summary.active ?? null,
    stopped: summary.stopped ?? null,
    groups,
  })
}

/** 自动弹窗判定：无历史卡或签名不同（新卡/内容变化）才弹；同卡重复投影不打扰。
 *  常驻「查看完整名单」按钮不走这里，任何时候都可手动打开。 */
export const shouldAutoOpenCandidateList = (
  previous: CandidateListCardData | undefined,
  next: CandidateListCardData,
): boolean => !previous || candidateListCardSignature(previous) !== candidateListCardSignature(next)
