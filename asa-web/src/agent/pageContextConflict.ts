import type { FloatingBridgeContext } from '../api'

export type CandidateContextIdentity = { id: string; label: string }
export type CandidatePageConflict = { task: CandidateContextIdentity; page: CandidateContextIdentity; stale: boolean; key: string }

const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' ? value as Record<string, unknown> : {}
const concreteId = (value: unknown) => {
  const id = String(value ?? '').trim()
  return id && id !== '0' && id !== 'null' && id !== 'undefined' ? id : ''
}
const candidateLabel = (focus: Record<string, unknown>, context: Record<string, unknown>) => {
  const candidate = record(focus.candidate)
  const name = candidate.name || context.candidate || focus.candidate_name
  if (name) return String(name)
  const job = record(focus.job)
  return [focus.client || context.client, context.job || job.title].filter(Boolean).join(' / ') || '当前任务人选'
}

export const candidateFocusIdentity = (focus?: Record<string, unknown> | null): CandidateContextIdentity | undefined => {
  if (!focus) return undefined
  const context = record(focus.context)
  const type = String(context.type || '').toLowerCase()
  const candidate = record(focus.candidate)
  const isCandidateFocus = type === 'candidate' || type === 'job_candidate' || Boolean(focus.job_candidate_id || context.job_candidate_id || candidate.id)
  if (!isCandidateFocus) return undefined
  const id = concreteId(context.id || focus.job_candidate_id || context.job_candidate_id || candidate.id)
  return id ? { id, label: candidateLabel(focus, context) } : undefined
}

export const candidatePageIdentity = (bridge?: FloatingBridgeContext): CandidateContextIdentity | undefined => {
  const id = concreteId(bridge?.job_candidate_id)
  return id ? { id, label: String(bridge?.title || bridge?.subtitle || '页面当前人选') } : undefined
}

export const compareCandidatePageContext = (focus?: Record<string, unknown> | null, bridge?: FloatingBridgeContext): CandidatePageConflict | undefined => {
  const task = candidateFocusIdentity(focus)
  const page = candidatePageIdentity(bridge)
  if (!task || !page || task.id === page.id) return undefined
  return { task, page, stale: bridge?.stale === true, key: `${task.id}:${page.id}:${bridge?.context_key || bridge?.instance_id || ''}` }
}
