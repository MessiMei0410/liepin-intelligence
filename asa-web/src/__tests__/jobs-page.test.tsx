import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Jobs } from '../pages/Jobs'
import type { Job } from '../api'

const makeJob = (id: number, extra: Partial<Job> = {}): Job => ({
  id,
  title: `岗位${id}`,
  client: 'ACME',
  location: '上海',
  status: '在推',
  lifecycle_stage: 'active_pipeline',
  priority: 'P1',
  candidate_count: id,
  active_candidate_count: id,
  updated_at: '2026-08-05T10:00:00',
  ...extra,
})

const rowTitles = () => screen.getAllByRole('row').slice(1).map(row => row.textContent)

describe('岗位列表 Jobs', () => {
  beforeEach(() => {
    // 页面筛选状态现在走 sessionStorage 持久化：用例间重置，避免互相污染。
    sessionStorage.clear()
  })

  it('默认 P0 视图展示对应岗位，各筛选按钮带联动计数', () => {
    const items = [
      makeJob(1, { priority: 'P0', lifecycle_stage: 'active_pipeline' }),
      makeJob(2, { priority: 'P0', lifecycle_stage: 'sourcing' }),
      makeJob(3, { priority: 'P0-最急', lifecycle_stage: 'client_feedback' }),
      makeJob(4, { priority: 'P1', lifecycle_stage: 'active_pipeline' }),
      makeJob(5, { priority: 'P2', lifecycle_stage: 'closed' }),
    ]
    render(<Jobs items={items} onSelect={() => {}} />)

    expect(screen.getByRole('row', { name: /岗位1/ })).toBeInTheDocument()
    expect(screen.getByRole('row', { name: /岗位3/ })).toBeInTheDocument()
    expect(screen.queryByRole('row', { name: /岗位4/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'P0 3' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: '在推 4' })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByRole('button', { name: '全部 5' })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByRole('status')).toHaveTextContent('共 3 个结果')

    fireEvent.click(screen.getByRole('button', { name: /^在推/ }))
    expect(screen.getByRole('row', { name: /岗位4/ })).toBeInTheDocument()
    expect(screen.queryByRole('row', { name: /岗位5/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '在推 4' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('status')).toHaveTextContent('共 4 个结果')

    fireEvent.click(screen.getByRole('button', { name: /^全部/ }))
    expect(screen.getAllByRole('row')).toHaveLength(6)
    expect(screen.getByRole('status')).toHaveTextContent('共 5 个结果')
  })

  it('搜索跨字段过滤并联动计数，无结果时显示可靠空态', () => {
    const items = [
      makeJob(1, { location: '杭州', priority: 'P0' }),
      makeJob(2, { location: '深圳', priority: 'P1' }),
      makeJob(3, { title: '销售总监', location: '上海', priority: 'P0' }),
    ]
    render(<Jobs items={items} onSelect={() => {}} />)
    const input = screen.getByLabelText('搜索岗位')
    fireEvent.click(screen.getByRole('button', { name: /^全部/ }))
    expect(screen.getByRole('status')).toHaveTextContent('共 3 个结果')

    fireEvent.change(input, { target: { value: '杭州' } })
    expect(screen.getByRole('row', { name: /岗位1/ })).toBeInTheDocument()
    expect(screen.queryByRole('row', { name: /岗位2/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '全部 1' })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('共 1 个结果')

    fireEvent.change(input, { target: { value: 'P0' } })
    expect(screen.getByRole('status')).toHaveTextContent('共 2 个结果')
    expect(screen.getByRole('button', { name: 'P0 2' })).toBeInTheDocument()

    fireEvent.change(input, { target: { value: '不存在的岗位' } })
    expect(screen.getByRole('status')).toHaveTextContent('共 0 个结果')
    expect(screen.getByText('没有符合当前条件的岗位。')).toBeInTheDocument()

    fireEvent.change(input, { target: { value: '' } })
    expect(screen.getByRole('status')).toHaveTextContent('共 3 个结果')
  })

  it('无筛选模型且池内有活跃人选的岗位显示警告标识', () => {
    const items = [
      makeJob(1, { priority: 'P0', filter_domain: 'power', filter_model_missing: false }),
      makeJob(2, { priority: 'P0', filter_domain: null, filter_model_missing: true }),
      makeJob(3, { priority: 'P0', filter_domain: null, filter_model_missing: false }),
    ]
    render(<Jobs items={items} onSelect={() => {}} />)

    const warned = screen.getByRole('row', { name: /岗位2/ })
    expect(warned).toHaveTextContent('无筛选模型')
    expect(screen.getByRole('row', { name: /岗位1/ })).not.toHaveTextContent('无筛选模型')
    expect(screen.getByRole('row', { name: /岗位3/ })).not.toHaveTextContent('无筛选模型')
  })

  it('有待确认筛选模型草稿的岗位显示待确认标识，且不与无模型警告混淆', () => {
    const items = [
      makeJob(1, { priority: 'P0', filter_domain: null, filter_model_missing: true }),
      makeJob(2, { priority: 'P0', filter_domain: null, filter_model_missing: false, filter_model_draft: true }),
      makeJob(3, { priority: 'P0', filter_domain: 'power', filter_model_missing: false }),
    ]
    render(<Jobs items={items} onSelect={() => {}} />)

    const draftRow = screen.getByRole('row', { name: /岗位2/ })
    expect(draftRow).toHaveTextContent('筛选模型待确认')
    expect(draftRow).not.toHaveTextContent('无筛选模型')
    expect(screen.getByRole('row', { name: /岗位1/ })).toHaveTextContent('无筛选模型')
    expect(screen.getByRole('row', { name: /岗位3/ })).not.toHaveTextContent('筛选模型待确认')
  })

  it('超过一页时分批展示，翻页与搜索后页码重置', () => {
    const items = Array.from({ length: 45 }, (_, index) => makeJob(index + 1, { priority: 'P0' }))
    render(<Jobs items={items} onSelect={() => {}} />)

    expect(screen.getAllByRole('row')).toHaveLength(21)
    expect(screen.queryByRole('row', { name: /岗位21/ })).not.toBeInTheDocument()
    expect(screen.getByText('第 1 / 3 页 · 显示 1–20 / 共 45 个')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '上一页' })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(screen.getByRole('row', { name: /岗位21/ })).toBeInTheDocument()
    expect(screen.getByText('第 2 / 3 页 · 显示 21–40 / 共 45 个')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('搜索岗位'), { target: { value: '岗位44' } })
    expect(screen.getByRole('status')).toHaveTextContent('共 1 个结果')
    expect(screen.getByRole('row', { name: /岗位44/ })).toBeInTheDocument()
    expect(screen.queryByText(/第 \d+ \/ \d+ 页/)).not.toBeInTheDocument()
  })

  it('外部数据收缩后页码自动夹回有效范围', () => {
    const items = Array.from({ length: 45 }, (_, index) => makeJob(index + 1, { priority: 'P0' }))
    const { rerender } = render(<Jobs items={items} onSelect={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(screen.getByText('第 2 / 3 页 · 显示 21–40 / 共 45 个')).toBeInTheDocument()

    rerender(<Jobs items={[makeJob(9, { priority: 'P0' })]} onSelect={() => {}} />)
    expect(screen.getByRole('status')).toHaveTextContent('共 1 个结果')
    expect(screen.getByRole('row', { name: /岗位9/ })).toBeInTheDocument()
    expect(screen.queryByText(/第 \d+ \/ \d+ 页/)).not.toBeInTheDocument()
  })

  it('排序稳定：默认更新时间倒序，缺失排最后，同值按 id 兜底', () => {
    const items = [
      makeJob(5, { priority: 'P0', updated_at: '2026-08-05T10:00:00' }),
      makeJob(10, { priority: 'P0', updated_at: '2026-08-05T10:00:00' }),
      makeJob(3, { priority: 'P0', updated_at: '2026-08-06T10:00:00' }),
      makeJob(7, { priority: 'P0', updated_at: undefined }),
      makeJob(1, { priority: 'P0', updated_at: '2026-08-04T10:00:00' }),
    ]
    render(<Jobs items={items} onSelect={() => {}} />)

    expect(rowTitles()[0]).toContain('岗位3')
    expect(rowTitles()[1]).toContain('岗位10')
    expect(rowTitles()[2]).toContain('岗位5')
    expect(rowTitles()[3]).toContain('岗位1')
    expect(rowTitles()[4]).toContain('岗位7')
  })

  it('可切换按活跃人选降序与客户/岗位排序', () => {
    const items = [
      makeJob(1, { client: 'Alpha', priority: 'P0', active_candidate_count: 3 }),
      makeJob(2, { client: 'Gamma', priority: 'P0', active_candidate_count: 9 }),
      makeJob(3, { client: 'Beta', priority: 'P0', active_candidate_count: 3 }),
    ]
    render(<Jobs items={items} onSelect={() => {}} />)

    fireEvent.change(screen.getByLabelText('排序方式'), { target: { value: 'active' } })
    expect(rowTitles()[0]).toContain('岗位2')
    expect(rowTitles()[1]).toContain('岗位3')
    expect(rowTitles()[2]).toContain('岗位1')

    fireEvent.change(screen.getByLabelText('排序方式'), { target: { value: 'name' } })
    expect(rowTitles()[0]).toContain('Alpha')
    expect(rowTitles()[1]).toContain('Beta')
    expect(rowTitles()[2]).toContain('Gamma')
  })

  it('表格具备 caption、scope 与 aria-sort，行可点击与键盘打开', () => {
    const onSelect = vi.fn()
    render(<Jobs items={[makeJob(7, { priority: 'P0' })]} onSelect={onSelect} />)

    expect(screen.getByRole('region', { name: '岗位列表，可横向滚动' })).toHaveAttribute('tabindex', '0')
    expect(screen.getByText('岗位列表')).toBeInTheDocument()
    const headers = screen.getAllByRole('columnheader')
    headers.forEach(header => expect(header).toHaveAttribute('scope', 'col'))
    expect(headers[4]).toHaveAttribute('aria-sort', 'descending')
    expect(headers[3]).not.toHaveAttribute('aria-sort')

    fireEvent.change(screen.getByLabelText('排序方式'), { target: { value: 'active' } })
    expect(headers[3]).toHaveAttribute('aria-sort', 'descending')
    expect(headers[4]).not.toHaveAttribute('aria-sort')

    const row = screen.getByRole('row', { name: /岗位7/ })
    fireEvent.click(row)
    expect(onSelect).toHaveBeenCalledWith(7)
    fireEvent.keyDown(row, { key: 'Enter' })
    expect(onSelect).toHaveBeenCalledTimes(2)
    fireEvent.keyDown(row, { key: ' ' })
    expect(onSelect).toHaveBeenCalledTimes(3)
  })

  it('空列表与无匹配模式都显示可靠空态', () => {
    const { rerender } = render(<Jobs items={[]} onSelect={() => {}} />)
    expect(screen.getByRole('status')).toHaveTextContent('共 0 个结果')
    expect(screen.getByText('没有符合当前条件的岗位。')).toBeInTheDocument()

    rerender(<Jobs items={[makeJob(1, { priority: 'P1', lifecycle_stage: 'closed' })]} onSelect={() => {}} />)
    expect(screen.getByRole('status')).toHaveTextContent('共 0 个结果')
    expect(screen.getByText('没有符合当前条件的岗位。')).toBeInTheDocument()
  })

  it('搜索/范围跨卸载与刷新保持（sessionStorage 持久化）', () => {
    const items = [makeJob(1, { priority: 'P0' }), makeJob(2, { priority: 'P1', lifecycle_stage: 'closed' })]
    const { unmount } = render(<Jobs items={items} onSelect={() => {}} />)
    // 切到「全部」并搜索「岗位2」，结果只剩岗位2
    fireEvent.click(screen.getByRole('button', { name: /全部 2/ }))
    fireEvent.change(screen.getByLabelText('搜索岗位'), { target: { value: '岗位2' } })
    expect(screen.getByText('岗位2')).toBeInTheDocument()
    expect(screen.queryByText('岗位1')).not.toBeInTheDocument()

    // 卸载（模拟切 tab）后重新挂载：搜索词与范围均保留
    unmount()
    render(<Jobs items={items} onSelect={() => {}} />)
    expect(screen.getByLabelText('搜索岗位')).toHaveValue('岗位2')
    expect(screen.getByText('岗位2')).toBeInTheDocument()
    expect(screen.queryByText('岗位1')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /全部 1/ })).toHaveAttribute('aria-pressed', 'true')
  })
})
