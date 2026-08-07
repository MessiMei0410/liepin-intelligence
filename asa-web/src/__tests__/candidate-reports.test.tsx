import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CandidatePanel } from '../panels/CandidatePanel'
import { candidateDetail, mockResponse } from './helpers'

describe('人选报告与产物', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('按版本展示报告并复用安全产物查看器', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async input => String(input).includes('/api/v1/artifacts/report-v2')
      ? mockResponse({
        ok: true,
        artifact: {
          artifact_id: 'report-v2', workflow_id: 'workflow-report', artifact_type: 'recommendation_report',
          title: '嘉驰推荐报告 v2', mime_type: 'text/markdown', content: '# 推荐结论\n\n建议顾问复核后发送。',
          content_size: 30, content_truncated: false, metadata: {}, validation_status: 'passed',
          downloadable: true, download_kind: 'content', file_name: 'report-v2.md',
          download_url: '/api/v1/artifacts/report-v2/file',
        },
      })
      : mockResponse({ ok: true })))
    const user = userEvent.setup()
    render(<CandidatePanel
      value={{
        ...candidateDetail,
        report_artifacts: [{
          id: 2, artifact_id: 'report-v2', workflow_id: 'workflow-report', artifact_type: 'recommendation_report',
          title: '嘉驰推荐报告 v2', validation_status: 'passed', version: 2,
        }],
      }}
      close={() => undefined}
      changed={() => undefined}
    />)

    expect(screen.getByText('推荐报告 v2 · 通过校验')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '查看人选产物：嘉驰推荐报告 v2' }))
    expect(await screen.findByRole('heading', { name: '推荐结论' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '下载完整产物' })).toHaveAttribute('href', '/api/v1/artifacts/report-v2/file')
  })
})
