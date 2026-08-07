import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { Workflow } from '../api'
import { WorkflowPanel } from '../workflows/WorkflowPanel'
import { artifactAbsenceMessage } from '../workflows/artifactPresentation'
import { mockResponse, plannedWorkflow } from './helpers'

const renderPanel = (workflow: Workflow) => render(
  <WorkflowPanel
    value={workflow}
    jobs={[]}
    close={() => undefined}
    reload={vi.fn()}
    openCandidate={() => undefined}
    archived={() => undefined}
  />,
)

describe('工作流产物查看', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('点击产物按需读取正文，显示中文类型和下载入口', async () => {
    const fetchMock = vi.fn<typeof fetch>(async input => {
      const url = String(input)
      if (url.includes('/api/v1/artifacts/artifact-1')) return mockResponse({
        ok: true,
        artifact: {
          artifact_id: 'artifact-1', workflow_id: 'wf-1', artifact_type: 'search_strategy',
          title: '多渠道寻访策略', mime_type: 'text/markdown', content: '# 结论\n\n优先搜索 VPD。',
          content_size: 28, content_truncated: false, metadata: {}, validation_status: 'passed',
          downloadable: true, download_kind: 'content', file_name: 'artifact-1.md',
          download_url: '/api/v1/artifacts/artifact-1/file',
        },
      })
      if (url.includes('/candidates')) return mockResponse({ ok: true, items: [], total: 0 })
      return mockResponse({ ok: true })
    })
    vi.stubGlobal('fetch', fetchMock)
    const workflow: Workflow = {
      ...plannedWorkflow,
      artifacts: [{
        artifact_id: 'artifact-1', title: '多渠道寻访策略', artifact_type: 'search_strategy',
        validation_status: 'passed', has_content: true,
      }],
    }
    const user = userEvent.setup()
    renderPanel(workflow)

    expect(screen.getByText('多渠道寻访策略 · 通过校验')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '查看产物：多渠道寻访策略' }))
    expect(await screen.findByRole('heading', { name: '多渠道寻访策略' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '结论' })).toBeInTheDocument()
    const download = screen.getByRole('link', { name: '下载完整产物' })
    expect(download).toHaveAttribute('href', '/api/v1/artifacts/artifact-1/file')
  })

  it('产物读取失败时显示站内错误回执', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).includes('/api/v1/artifacts/')
      ? mockResponse({ error: '产物不存在' }, false, 404)
      : mockResponse({ ok: true, items: [], total: 0 })))
    const user = userEvent.setup()
    renderPanel({
      ...plannedWorkflow,
      artifacts: [{ artifact_id: 'missing', title: '执行回执', artifact_type: 'external_action_receipt', validation_status: 'passed' }],
    })
    await user.click(screen.getByRole('button', { name: '查看产物：执行回执' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('产物不存在')
  })

  it('无文件产物时解释结果去向，不再只显示暂无产物', () => {
    const workflow: Workflow = {
      ...plannedWorkflow,
      workflow: { workflow_id: 'wf-assessment', status: 'completed' },
      artifact_summary: { kind: 'business_records', count: 0, message: '本轮结果已写入候选人评估记录，不另生成文件产物。' },
      artifacts: [],
    }
    renderPanel(workflow)
    expect(screen.getByText('本轮结果已写入候选人评估记录，不另生成文件产物。')).toBeInTheDocument()
    expect(screen.queryByText('暂无执行产物')).not.toBeInTheDocument()
  })

  it('旧 payload 没有服务端摘要时按状态给出稳定解释', () => {
    expect(artifactAbsenceMessage({
      ...plannedWorkflow,
      workflow: { workflow_id: 'wf-cancelled', status: 'cancelled' },
    })).toBe('工作流已取消；取消前没有生成可查看产物。')
  })
})
