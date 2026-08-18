import { describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { CandidateListDialog } from '../agent/CandidateListDialog'
import type { CandidateListCardData } from '../workflows/CandidateListCard'

const data: CandidateListCardData = {
  type: 'candidate_list',
  title: '长越科技｜机械高级工程师（岗位 137）候选名单',
  context: { type: 'job', id: 137 },
  summary: { total: 5, active: 3, stopped: 2, bonder_count: 1 },
  groups: [
    {
      key: 'bonder',
      label: '固晶机/共晶机/键合机背景',
      priority: true,
      candidates: [{ id: 522, name: '张航', company: 'ASM中国集团公司', title: '高级机械设计工程师', stage: '已触达' }],
    },
    {
      key: 'active',
      label: '其余可推进候选',
      priority: false,
      candidates: [{ id: 519, name: '陈**', company: '先导科技', title: '结构设计工程师', stage: 'S1 新增寻访/待复核' }],
    },
    {
      key: 'stopped',
      label: '已停止推进',
      priority: false,
      candidates: [{ id: 511, name: '刘先生', company: '上海泽丰', title: '机械工程师', stage: 'H5 初筛不通过' }],
    },
  ],
}

describe('CandidateListDialog', () => {
  it('渲染完整名单（全量不截断）与统计', () => {
    render(<CandidateListDialog data={data} onOpenCandidate={() => {}} onClose={() => {}} />)
    expect(screen.getByText('长越科技｜机械高级工程师（岗位 137）候选名单')).toBeTruthy()
    expect(screen.getByText(/共 5 人/)).toBeTruthy()
    expect(screen.getByText(/固晶\/共晶\/键合背景 1 人/)).toBeTruthy()
    // 三个分组都渲染
    expect(screen.getByText(text => text.includes('固晶机/共晶机/键合机背景'))).toBeTruthy()
    expect(screen.getByText('其余可推进候选')).toBeTruthy()
    expect(screen.getByText('已停止推进')).toBeTruthy()
  })

  it('点击人选行触发 onOpenCandidate 带人选 id 与名单顺序', () => {
    const onOpenCandidate = vi.fn()
    render(<CandidateListDialog data={data} onOpenCandidate={onOpenCandidate} onClose={() => {}} />)
    fireEvent.click(screen.getByText('张航'))
    expect(onOpenCandidate).toHaveBeenCalledWith(522, [522, 519, 511])
  })

  it('Escape 触发 onClose', () => {
    const onClose = vi.fn()
    render(<CandidateListDialog data={data} onOpenCandidate={() => {}} onClose={onClose} />)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })

  it('点击关闭按钮触发 onClose', () => {
    const onClose = vi.fn()
    render(<CandidateListDialog data={data} onOpenCandidate={() => {}} onClose={onClose} />)
    fireEvent.click(screen.getByLabelText('关闭名单'))
    expect(onClose).toHaveBeenCalled()
  })

  it('提供打开岗位入口', () => {
    const onOpenJob = vi.fn()
    render(<CandidateListDialog data={data} onOpenCandidate={() => {}} onOpenJob={onOpenJob} onClose={() => {}} />)
    fireEvent.click(screen.getByText('打开岗位查看完整名单'))
    expect(onOpenJob).toHaveBeenCalledWith(137)
  })

  it('弹出独立窗口只带名单数据（有候选人时不带岗位 url）', () => {
    const messages: Array<Record<string, unknown>> = []
    const w = window as unknown as { webkit?: unknown }
    w.webkit = { messageHandlers: { asaNative: { postMessage: (msg: Record<string, unknown>) => messages.push(msg) } } }
    const onClose = vi.fn()
    render(<CandidateListDialog data={data} onOpenCandidate={() => {}} onOpenJob={() => {}} onClose={onClose} />)
    fireEvent.click(screen.getByLabelText('弹出为独立窗口'))
    expect(messages).toHaveLength(1)
    expect(messages[0].type).toBe('openDetachedDialog')
    const list = messages[0].list as { groups?: unknown[] } | undefined
    expect(Array.isArray(list?.groups)).toBe(true)
    expect('url' in messages[0]).toBe(false)
    expect(onClose).toHaveBeenCalled()
    delete w.webkit
  })

  it('无候选数据时弹出独立窗口退回岗位页 url', () => {
    const messages: Array<Record<string, unknown>> = []
    const w = window as unknown as { webkit?: unknown }
    w.webkit = { messageHandlers: { asaNative: { postMessage: (msg: Record<string, unknown>) => messages.push(msg) } } }
    const empty: CandidateListCardData = { type: 'candidate_list', title: '空名单', context: { type: 'job', id: 137 }, groups: [] }
    render(<CandidateListDialog data={empty} onOpenCandidate={() => {}} onOpenJob={() => {}} onClose={() => {}} />)
    fireEvent.click(screen.getByLabelText('弹出为独立窗口'))
    expect(messages).toHaveLength(1)
    expect(messages[0].url).toBe('/asa-app#job=137&bare=1')
    expect('list' in messages[0]).toBe(false)
    delete w.webkit
  })
})
