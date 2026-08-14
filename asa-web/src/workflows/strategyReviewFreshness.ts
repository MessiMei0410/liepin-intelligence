import { parseTimestamp, isStaleAgainst } from '../shared/freshness'

export type StrategyReviewFreshness = {
  stale: boolean
  generatedAt?: number
  workflowUpdatedAt?: number
}

export const strategyReviewFreshness = (generatedAt?: string, workflowUpdatedAt?: string): StrategyReviewFreshness => {
  const generated = parseTimestamp(generatedAt)
  const workflowUpdated = parseTimestamp(workflowUpdatedAt)
  return {
    stale: isStaleAgainst(generatedAt, workflowUpdatedAt),
    generatedAt: generated,
    workflowUpdatedAt: workflowUpdated,
  }
}
