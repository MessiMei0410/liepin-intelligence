import { describe, expect, it } from 'vitest'
import { recommendationPackageFreshness } from '../panels/recommendationPackageFreshness'

describe('recommendation package freshness', () => {
  it('marks a package stale when its current assessment is newer', () => {
    expect(recommendationPackageFreshness('2026-08-05 10:00:00', '2026-08-06 09:00:00').stale).toBe(true)
    expect(recommendationPackageFreshness('2026-08-06 09:00:00', '2026-08-05 10:00:00').stale).toBe(false)
  })

  it('does not infer staleness when either timestamp is missing or invalid', () => {
    expect(recommendationPackageFreshness('', '2026-08-06 09:00:00').stale).toBe(false)
    expect(recommendationPackageFreshness('not-a-date', '2026-08-06 09:00:00').stale).toBe(false)
  })
})
