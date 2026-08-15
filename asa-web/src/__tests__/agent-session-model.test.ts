import { describe, expect, it } from 'vitest'
import { parseAgentSession, parseAgentSessionList, parseAgentSessionSearch } from '../agent/sessionModel'

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

  it('parses history paging fields and falls back for legacy Core payloads', () => {
    const paged = parseAgentSession({ ok: true, session_id: 'task-1', messages: [], total: 120, has_more: true })
    expect(paged.total).toBe(120)
    expect(paged.has_more).toBe(true)
    // 旧 Core 无 offset 契约时按 0/false 兜底，前端不误报更早历史。
    const legacy = parseAgentSession({ ok: true, session_id: 'task-1', messages: [] })
    expect(legacy.total).toBe(0)
    expect(legacy.has_more).toBe(false)
  })

  it('parses message search anchors for jump-to-message', () => {
    const result = parseAgentSessionSearch({
      ok: true,
      session_id: 'task-1',
      query: '张航',
      total: 6,
      matches: [{ role: 'user', created_at: '2026-08-15 09:00', content: '候选人张航在杭州', snippet: '候选人张航在杭州', newer_count: 5 }],
    })
    expect(result.matches[0].newer_count).toBe(5)
    expect(result.matches[0].role).toBe('user')
    // 旧 Core 无搜索端点时字段缺失应拒绝（role 非法）。
    expect(() => parseAgentSessionSearch({ ok: true, session_id: 'task-1', query: 'x', matches: [{ role: 'tool', newer_count: 0 }] })).toThrow()
  })
})
