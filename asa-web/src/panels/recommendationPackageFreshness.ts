import { parseTimestamp, isStaleAgainst } from '../shared/freshness'

export type RecommendationPackageFreshness = {
  stale: boolean
  packageAt?: number
  assessmentAt?: number
}

export const recommendationPackageFreshness = (packageCreatedAt?: string, assessmentAt?: string): RecommendationPackageFreshness => {
  const packageTime = parseTimestamp(packageCreatedAt)
  const assessmentTime = parseTimestamp(assessmentAt)
  return {
    stale: isStaleAgainst(packageCreatedAt, assessmentAt),
    packageAt: packageTime,
    assessmentAt: assessmentTime,
  }
}
