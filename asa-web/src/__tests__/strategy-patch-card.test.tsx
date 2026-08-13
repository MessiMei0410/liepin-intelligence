import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import { StrategyPatchCard } from '../agent/StrategyPatchCard'

const patch = {
  workflow_id: 'workflow_1', workflow_title: '长越科技｜机械高级工程师｜第 2 轮', strategy_hash: 'hash_v2',
  changes: [
    { type: 'add_keyword', value: '精密运动平台', clause: '新增关键词「精密运动平台」' },
    { type: 'add_company', value: 'ASMPT', clause: '新增对标公司「ASMPT」' },
    { type: 'add_filter', value: '排除纯销售背景', clause: '新增过滤条件「排除纯销售背景」' },
  ],
}

describe('主 Agent 策略建议沉淀卡', () => {
  afterEach(() => vi.restoreAllMocks())

  it('逐项选择并二次确认后，携带策略 hash 确定性写入并记录会话事件', async () => {
    const preflight = vi.spyOn(api, 'preflightStrategyEdits').mockResolvedValue({
      ok: true, workflow_id: 'workflow_1', strategy_hash: 'hash_v2', preflight_token: 'token_1',
      impact: '确认后只写入所列策略项，不启动寻访',
    })
    const apply = vi.spyOn(api, 'applyStrategyEdits').mockResolvedValue({
      ok: true, workflow_id: 'workflow_1', revision: 3, edit_count: 2, artifact_id: 'artifact_3',
    })
    const event = vi.spyOn(api, 'recordCopilotEvent').mockResolvedValue({ ok: true })
    render(<StrategyPatchCard patch={patch} sessionId="session_1"/>)

    fireEvent.click(screen.getByRole('checkbox', { name: /排除纯销售背景/ }))
    fireEvent.click(screen.getByRole('button', { name: '检查写入内容' }))
    await waitFor(() => expect(preflight).toHaveBeenCalled())
    expect(screen.getByText(/新增关键词「精密运动平台」/)).toBeInTheDocument()
    expect(screen.queryByText(/排除纯销售背景/)).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '确认写入' }))
    await waitFor(() => expect(apply).toHaveBeenCalledWith('workflow_1', [
      { op: 'append_keyword_terms', group: '顾问对话确认', terms: ['精密运动平台'], targets: '顾问确认的核心能力词' },
      { op: 'add_company', tier: 'T1', name: 'ASMPT', source: 'consultant_confirmed', confidence: 'high' },
    ], 'ASA 主对话中由顾问逐项确认', 'hash_v2', 'token_1'))
    expect(await screen.findByText(/已将 2 项顾问确认写入策略 revision 3/)).toBeInTheDocument()
    expect(event).toHaveBeenCalledWith('session_1', 'copilot_strategy_applied', expect.objectContaining({
      workflow_id: 'workflow_1', revision: 3, artifact_id: 'artifact_3', applied: 2,
    }))
  })

  it('策略版本漂移时保留确认界面并展示服务端冲突', async () => {
    vi.spyOn(api, 'preflightStrategyEdits').mockResolvedValue({
      ok: true, workflow_id: 'workflow_1', strategy_hash: 'hash_v2', preflight_token: 'token_1',
    })
    vi.spyOn(api, 'applyStrategyEdits').mockRejectedValue(new Error('寻访策略已更新，本次建议基于旧版本；请刷新对话后重新确认'))
    render(<StrategyPatchCard patch={patch} sessionId="session_1"/>)
    fireEvent.click(screen.getByRole('button', { name: '检查写入内容' }))
    await screen.findByText('预检通过，令牌有效期 5 分钟')
    fireEvent.click(screen.getByRole('button', { name: '确认写入' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('寻访策略已更新')
    expect(screen.getByRole('button', { name: '确认写入' })).toBeEnabled()
  })

  it('会话恢复后展示已沉淀 revision，不重复提供写入动作', () => {
    render(<StrategyPatchCard patch={patch} sessionId="session_1" applied appliedRevision={4} appliedCount={3}/>)
    expect(screen.getByText(/已将 3 项顾问确认写入策略 revision 4/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '确认写入' })).not.toBeInTheDocument()
  })

  it('策略写入成功但会话同步失败时保留成功回执并允许幂等重试', async () => {
    vi.spyOn(api, 'preflightStrategyEdits').mockResolvedValue({
      ok: true, workflow_id: 'workflow_1', strategy_hash: 'hash_v2', preflight_token: 'token_1',
    })
    vi.spyOn(api, 'applyStrategyEdits').mockResolvedValue({
      ok: true, workflow_id: 'workflow_1', revision: 5, edit_count: 3, artifact_id: 'artifact_5',
    })
    const event = vi.spyOn(api, 'recordCopilotEvent')
      .mockRejectedValueOnce(new Error('连接中断'))
      .mockResolvedValueOnce({ ok: true })
    render(<StrategyPatchCard patch={patch} sessionId="session_1"/>)

    fireEvent.click(screen.getByRole('button', { name: '检查写入内容' }))
    await screen.findByText('预检通过，令牌有效期 5 分钟')
    fireEvent.click(screen.getByRole('button', { name: '确认写入' }))

    expect(await screen.findByText(/已将 3 项顾问确认写入策略 revision 5/)).toBeInTheDocument()
    expect(await screen.findByRole('alert')).toHaveTextContent('策略已写入，但会话记录同步失败')
    fireEvent.click(screen.getByRole('button', { name: '重试同步会话记录' }))
    await waitFor(() => expect(event).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  })
})
