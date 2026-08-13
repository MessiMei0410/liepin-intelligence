import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ExecutionReceipt, SuggestedActionBar, UnderstandingCard } from '../agent/AgentInteractionCards'

describe('Agent interaction cards', () => {
  it('理解卡展示对象、目标、判断和无选项澄清问题', () => {
    render(<UnderstandingCard card={{
      show: true, confidence: 0.72, action_label: '比较候选人', objective: '找出前 5 名',
      target: { client: '长越科技', job: '机械高级工程师', candidate: '王先生' },
      key_judgment: '证据不足的人选不进入 A 级', clarification_question: '是否保留地域限制？',
    }}/>)
    const card = screen.getByRole('region', { name: 'ASA 理解卡' })
    expect(card).toHaveTextContent('长越科技')
    expect(card).toHaveTextContent('找出前 5 名')
    expect(card).toHaveTextContent('证据不足的人选不进入 A 级')
    expect(card).toHaveTextContent('是否保留地域限制？')
  })

  it('结构化动作矩阵可直达 handler', () => {
    const onAction = vi.fn()
    render(<SuggestedActionBar onAction={onAction} actions={[
      { type: 'view_a_candidates', label: '查看 A 级人选' },
      { type: 'compare_top_candidates', label: '比较前 5 人' },
      { type: 'continue_sourcing', label: '按当前条件继续搜' },
      { type: 'generate_contact_queue', label: '生成触达队列' },
      { type: 'confirm_advance', label: '确认推进' },
      { type: 'end_round', label: '结束本轮' },
    ]}/>)
    fireEvent.click(screen.getByRole('button', { name: '比较前 5 人' }))
    expect(onAction).toHaveBeenCalledWith(expect.objectContaining({ type: 'compare_top_candidates' }))
  })

  it('执行回执显示数量、范围、失败原因和下一步', () => {
    render(<ExecutionReceipt receipt={{
      state: '部分完成', summary: '本轮完成 4 人', succeeded: 4, skipped: 1, failed: 2,
      failure_reason: '2 人缺少联系方式', scope: { label: '机械高级工程师候选池' },
      verified: true, next_step: '复核失败人选',
    }}/>)
    const card = screen.getByRole('region', { name: '执行回执' })
    expect(card).toHaveTextContent('成功 4')
    expect(card).toHaveTextContent('失败 2')
    expect(card).toHaveTextContent('机械高级工程师候选池')
    expect(card).toHaveTextContent('复核失败人选')
  })
})
