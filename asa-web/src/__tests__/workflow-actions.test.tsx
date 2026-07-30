import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { WorkflowPanel } from '../workflows/WorkflowPanel'
import { mockResponse, plannedWorkflow } from './helpers'

const renderPanel = (reload = vi.fn()) => {
  render(<WorkflowPanel value={plannedWorkflow} jobs={[]} close={() => undefined} reload={reload} openCandidate={() => undefined} archived={() => undefined} />)
  return { reload }
}

describe('工作流动作反馈', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('点击启动后按钮立即进入 loading/disabled，完成后原地刷新', async () => {
    let release: (value: Response) => void = () => undefined
    // R7：动作成功后先打 summary 再按需 reload；summary 调用立即应答，动作调用仍由 release 控制以覆盖 in-flight 状态。
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      void init
      return String(input).includes('/summary')
        ? Promise.resolve(mockResponse({ ok: true }))
        : new Promise<Response>(resolve => { release = resolve })
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    const { reload } = renderPanel()
    const start = screen.getByRole('button', { name: '确认计划并准备' })
    await user.click(start)
    expect(start).toBeDisabled()
    expect(screen.getByRole('button', { name: '取消' })).toBeDisabled()
    release(mockResponse({ ok: true }))
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1))
    expect(screen.getByRole('button', { name: '确认计划并准备' })).toBeEnabled()
    const startCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/wf-1/start'))
    const body = JSON.parse(String((startCall?.[1] as RequestInit).body))
    expect(body).toMatchObject({ expected_plan_version: 1, expected_plan_hash: 'plan-hash-1' })
  })

  it('接口失败时显示可读错误文案且按钮恢复可用', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => mockResponse({ detail: '工作流已在运行中' }, false, 409)))
    const user = userEvent.setup()
    renderPanel()
    const start = screen.getByRole('button', { name: '确认计划并准备' })
    await user.click(start)
    expect(await screen.findByText('工作流已在运行中')).toBeInTheDocument()
    expect(start).toBeEnabled()
  })

  it('R3 寻访审批展示检索轴、评估约束和非平台筛选语义', () => {
    const value = {
      ...plannedWorkflow,
      approvals: [{
        approval_id: 'approval-sourcing',
        title: '执行多渠道寻访',
        risk_level: 'R3',
        status: 'pending',
        created_at: '2026-07-30 10:00:00',
        preflight: {
          channel: 'Liepin + X-SaaS',
          query_plan_v1: {
            cell_count: 163,
            dimensions: {
              locations: ['杭州'],
              levels: ['资深工程师'],
              scenarios: ['运动控制'],
            },
            execution_semantics: {
              retrieval_axes: ['channel', 'query'],
              platform_filters: [],
            },
          },
        },
      }],
    }

    render(<WorkflowPanel value={value} jobs={[]} close={() => undefined} reload={() => undefined} openCandidate={() => undefined} archived={() => undefined} />)

    const scope = screen.getByLabelText('寻访执行范围')
    expect(scope).toHaveTextContent('渠道 + 关键词（163 个查询单元）')
    expect(scope).toHaveTextContent('地点：杭州')
    expect(scope).toHaveTextContent('职级：资深工程师')
    expect(scope).toHaveTextContent('场景：运动控制')
    expect(scope).toHaveTextContent('不作为平台筛选')
    expect(screen.getByRole('button', { name: '批准本次外部寻访' })).toBeInTheDocument()
  })
})
