import { describe, expect, it } from 'vitest'
import { parseAgentSession, parseAgentSessionList } from '../agent/sessionModel'

describe('Agent session boundary', () => {
  it('parses persisted tasks and structured message references', () => {
    const result = parseAgentSession({
      ok: true,
      session_id: 'task-1',
      business_focus: { context: { type: 'job', id: 154 } },
      messages: [{
        role: 'assistant',
        content: '已找到候选人',
        references: [{ type: 'candidate', id: 559, label: '衣**' }],
      }],
    })
    expect(result.messages[0].references?.[0]).toEqual({ type: 'candidate', id: 559, label: '衣**' })
  })

  it('rejects drifted list and message payloads', () => {
    expect(() => parseAgentSessionList({ ok: true, sessions: [{ session_id: 'task-1' }] })).toThrow()
    expect(() => parseAgentSession({ ok: true, session_id: 'task-1', messages: [{ role: 'tool', content: 'x' }] })).toThrow()
  })
})
