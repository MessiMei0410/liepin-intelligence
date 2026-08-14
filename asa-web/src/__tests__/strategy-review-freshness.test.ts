import { describe, expect, it } from 'vitest'
import { strategyReviewFreshness } from '../workflows/strategyReviewFreshness'

describe('strategy review freshness', () => {
  it('marks a review stale only when the workflow changed afterwards', () => {
    expect(strategyReviewFreshness('2026-08-14 10:00:00', '2026-08-14 10:01:00').stale).toBe(true)
    expect(strategyReviewFreshness('2026-08-14 10:01:00', '2026-08-14 10:00:00').stale).toBe(false)
  })

  it('does not infer freshness from missing or invalid timestamps', () => {
    expect(strategyReviewFreshness('', '2026-08-14 10:00:00').stale).toBe(false)
    expect(strategyReviewFreshness('not-a-date', '2026-08-14 10:00:00').stale).toBe(false)
  })
})
