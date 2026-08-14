import { describe, expect, it } from 'vitest'
import { candidateFocusIdentity, compareCandidatePageContext } from '../agent/pageContextConflict'

describe('page context candidate conflict', () => {
  const bridge = { surface: 'liepin', job_candidate_id: 116, title: '候选人 B', context_key: 'liepin:tab-1' }

  it('only compares concrete candidate identities', () => {
    expect(candidateFocusIdentity({ context: { type: 'job', id: 154 } })).toBeUndefined()
    expect(compareCandidatePageContext({ context: { type: 'job', id: 154 } }, bridge)).toBeUndefined()
  })

  it('does not warn for the same candidate', () => {
    expect(compareCandidatePageContext({ context: { type: 'candidate', id: 116 }, candidate: { name: '候选人 B' } }, bridge)).toBeUndefined()
  })

  it('returns a conflict for different candidates and keeps stale state', () => {
    expect(compareCandidatePageContext({ context: { type: 'job_candidate', id: 115 }, candidate: { name: '候选人 A' } }, { ...bridge, stale: true })).toMatchObject({
      task: { id: '115', label: '候选人 A' }, page: { id: '116', label: '候选人 B' }, stale: true,
    })
  })
})
