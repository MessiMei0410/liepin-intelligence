import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Candidates } from '../pages/Candidates'
import type { Candidate } from '../api'

const makeCandidate = (id: number, extra: Partial<Candidate> = {}): Candidate => ({
  id,
  person_id: id,
  name: `候选人${id}`,
  client: 'ACME',
  job: '前端工程师',
  current_company: '示例科技',
  current_title: '高级工程师',
  source_type: 'liepin',
  clean_stage: 'S1 待复核',
  flow_bucket: '初筛',
  updated_at: '2026-08-05T10:00:00',
  ...extra,
})

const rowNames = () => screen.getAllByRole('row').slice(1).map(row => row.textContent)

describe('候选人列表 Candidates', () => {
  it('未超页时一次展示全部，并显示总数', () => {
    render(<Candidates items={[makeCandidate(1), makeCandidate(2)]} openCandidate={() => {}} />)
    expect(screen.getAllByRole('row')).toHaveLength(3)
    expect(screen.getByRole('status')).toHaveTextContent('共 2 个结果')
    expect(screen.queryByText(/第 1 \/ 1 页/)).not.toBeInTheDocument()
  })

  it('默认按更新时间新→旧稳定排序，超过一页时分批显示并可翻页', () => {
    const openCandidate = vi.fn()
    const items = Array.from({ length: 25 }, (_, index) => makeCandidate(index + 1))
    render(<Candidates items={items} openCandidate={openCandidate} />)

    // 同一更新时间下按 id 倒序兜底：第一页 25–6，第二页 5–1。
    expect(screen.getAllByRole('row')).toHaveLength(21)
    expect(screen.getByText('候选人25')).toBeInTheDocument()
    expect(screen.queryByText('候选人5')).not.toBeInTheDocument()
    expect(screen.getByText('第 1 / 2 页 · 显示 1–20 / 共 25 个')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '上一页' })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(screen.getAllByRole('row')).toHaveLength(6)
    expect(screen.getByText('候选人5')).toBeInTheDocument()
    expect(screen.queryByText('候选人25')).not.toBeInTheDocument()
    expect(screen.getByText('第 2 / 2 页 · 显示 21–25 / 共 25 个')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: '上一页' }))
    expect(screen.getByText('候选人25')).toBeInTheDocument()
    expect(screen.queryByText('候选人5')).not.toBeInTheDocument()
  })

  it('点击行、回车与空格都能打开候选人', () => {
    const openCandidate = vi.fn()
    render(<Candidates items={[makeCandidate(7)]} openCandidate={openCandidate} />)
    const row = screen.getByRole('row', { name: /候选人7/ })

    fireEvent.click(row)
    expect(openCandidate).toHaveBeenCalledWith(7)

    fireEvent.keyDown(row, { key: 'Enter' })
    expect(openCandidate).toHaveBeenCalledTimes(2)

    fireEvent.keyDown(row, { key: ' ' })
    expect(openCandidate).toHaveBeenCalledTimes(3)
  })

  it('范围切换后重置到第一页，空范围显示空态，计数联动', () => {
    const active = Array.from({ length: 45 }, (_, index) => makeCandidate(index + 1))
    const stopped = Array.from({ length: 5 }, (_, index) => makeCandidate(100 + index, { clean_stage: '已停止' }))
    render(<Candidates items={[...active, ...stopped]} openCandidate={() => {}} />)

    expect(screen.getByRole('button', { name: '待处理 45' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: '已停止 5' })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByRole('status')).toHaveTextContent('共 45 个结果')
    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(screen.getByText('候选人21')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /^已停止/ }))
    expect(screen.getByRole('status')).toHaveTextContent('共 5 个结果')
    expect(screen.getByText('候选人101')).toBeInTheDocument()
    expect(screen.queryByText('候选人1')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /^待处理/ }))
    expect(screen.getByRole('status')).toHaveTextContent('共 45 个结果')
    expect(screen.getByText('候选人45')).toBeInTheDocument()
    expect(screen.queryByText('候选人21')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /^全部/ }))
    expect(screen.getByRole('status')).toHaveTextContent('共 50 个结果')
    expect(screen.getByText('第 1 / 3 页 · 显示 1–20 / 共 50 个')).toBeInTheDocument()
  })

  it('某范围无结果时显示可靠空态', () => {
    render(<Candidates items={[makeCandidate(1)]} openCandidate={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: /^已停止/ }))
    expect(screen.getByRole('status')).toHaveTextContent('共 0 个结果')
    expect(screen.getByText('没有符合当前条件的候选人。')).toBeInTheDocument()
  })

  it('搜索跨字段过滤并联动计数与页码重置，无结果时显示空态', () => {
    const items = [
      makeCandidate(1, { current_company: '字节跳动', city: '上海' }),
      makeCandidate(2, { client: '腾讯', current_title: '产品经理', source_type: 'xsaas' }),
      makeCandidate(3, { job: '销售总监', clean_stage: '已停止' }),
    ]
    render(<Candidates items={items} openCandidate={() => {}} />)
    const input = screen.getByLabelText('搜索候选人')
    fireEvent.click(screen.getByRole('button', { name: /^全部/ }))
    expect(screen.getByRole('button', { name: '全部 3' })).toBeInTheDocument()

    fireEvent.change(input, { target: { value: '字节' } })
    expect(screen.getByRole('row', { name: /候选人1/ })).toBeInTheDocument()
    expect(screen.queryByRole('row', { name: /候选人2/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '全部 1' })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('共 1 个结果')

    fireEvent.change(input, { target: { value: '产品经理' } })
    expect(screen.getByRole('row', { name: /候选人2/ })).toBeInTheDocument()
    expect(screen.queryByRole('row', { name: /候选人1/ })).not.toBeInTheDocument()

    // 渠道标签与阶段文本也可搜索
    fireEvent.change(input, { target: { value: '猎聘' } })
    expect(screen.getByRole('status')).toHaveTextContent('共 2 个结果')
    fireEvent.change(input, { target: { value: 'X-SaaS' } })
    expect(screen.getByRole('status')).toHaveTextContent('共 1 个结果')
    fireEvent.change(input, { target: { value: '待复核' } })
    expect(screen.getByRole('status')).toHaveTextContent('共 2 个结果')

    fireEvent.change(input, { target: { value: '不存在的候选人' } })
    expect(screen.getByRole('status')).toHaveTextContent('共 0 个结果')
    expect(screen.getByText('没有符合当前条件的候选人。')).toBeInTheDocument()

    fireEvent.change(input, { target: { value: '' } })
    expect(screen.getByRole('status')).toHaveTextContent('共 3 个结果')
  })

  it('Mapping 入库关系显示并可按真实来源检索，不误标为人才库', () => {
    render(<Candidates items={[makeCandidate(9, { source_type: 'mapping' })]} openCandidate={() => {}} />)

    expect(screen.getByText('Mapping 直挖')).toBeInTheDocument()
    expect(screen.queryByText('人才库')).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('搜索候选人'), { target: { value: 'Mapping 直挖' } })
    expect(screen.getByRole('status')).toHaveTextContent('共 1 个结果')
    expect(screen.getByRole('row', { name: /候选人9/ })).toBeInTheDocument()
  })

  it('搜索与范围变化后页码回到第一页', () => {
    const items = Array.from({ length: 45 }, (_, index) => makeCandidate(index + 1))
    render(<Candidates items={items} openCandidate={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(screen.getByText('第 2 / 3 页 · 显示 21–40 / 共 45 个')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('搜索候选人'), { target: { value: '候选人40' } })
    expect(screen.getByRole('status')).toHaveTextContent('共 1 个结果')
    expect(screen.getByRole('row', { name: /候选人40/ })).toBeInTheDocument()
    expect(screen.queryByText(/第 \d+ \/ \d+ 页/)).not.toBeInTheDocument()
  })

  it('外部数据收缩后页码状态被夹回，列表回涨时不会跳回旧页', () => {
    const items = Array.from({ length: 45 }, (_, index) => makeCandidate(index + 1))
    const { rerender } = render(<Candidates items={items} openCandidate={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(screen.getByText('第 2 / 3 页 · 显示 21–40 / 共 45 个')).toBeInTheDocument()

    rerender(<Candidates items={[makeCandidate(9)]} openCandidate={() => {}} />)
    expect(screen.getByRole('status')).toHaveTextContent('共 1 个结果')
    expect(screen.getByRole('row', { name: /候选人9/ })).toBeInTheDocument()
    expect(screen.queryByText(/第 \d+ \/ \d+ 页/)).not.toBeInTheDocument()

    rerender(<Candidates items={items} openCandidate={() => {}} />)
    expect(screen.getByText('第 1 / 3 页 · 显示 1–20 / 共 45 个')).toBeInTheDocument()
    expect(screen.getByText('候选人45')).toBeInTheDocument()
  })

  it('排序稳定：默认更新时间倒序、缺失最后、同值按 id 兜底', () => {
    const items = [
      makeCandidate(5, { updated_at: '2026-08-05T10:00:00' }),
      makeCandidate(10, { updated_at: '2026-08-05T10:00:00' }),
      makeCandidate(3, { updated_at: '2026-08-06T10:00:00' }),
      makeCandidate(7, { updated_at: undefined }),
      makeCandidate(1, { updated_at: '2026-08-04T10:00:00' }),
    ]
    render(<Candidates items={items} openCandidate={() => {}} />)

    expect(rowNames()[0]).toContain('候选人3')
    expect(rowNames()[1]).toContain('候选人10')
    expect(rowNames()[2]).toContain('候选人5')
    expect(rowNames()[3]).toContain('候选人1')
    expect(rowNames()[4]).toContain('候选人7')
  })

  it('可切换按姓名与阶段排序', () => {
    const items = [
      makeCandidate(1, { name: 'Alice', clean_stage: '初筛' }),
      makeCandidate(2, { name: 'Carol', clean_stage: '复试' }),
      makeCandidate(3, { name: 'Bob', clean_stage: '已停止' }),
    ]
    render(<Candidates items={items} openCandidate={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: /^全部/ }))

    fireEvent.change(screen.getByLabelText('排序方式'), { target: { value: 'name' } })
    expect(rowNames()[0]).toContain('Alice')
    expect(rowNames()[1]).toContain('Bob')
    expect(rowNames()[2]).toContain('Carol')

    fireEvent.change(screen.getByLabelText('排序方式'), { target: { value: 'stage' } })
    expect(rowNames()[0]).toContain('初筛')
    expect(rowNames()[1]).toContain('复试')
    expect(rowNames()[2]).toContain('已停止')
  })

  it('表格具备 caption、scope 与 aria-sort，范围按钮带 aria-pressed', () => {
    render(<Candidates items={[makeCandidate(1), makeCandidate(2)]} openCandidate={() => {}} />)

    expect(screen.getByRole('region', { name: '候选人列表，可横向滚动' })).toHaveAttribute('tabindex', '0')
    expect(screen.getByText('候选人列表')).toBeInTheDocument()
    const headers = screen.getAllByRole('columnheader')
    headers.forEach(header => expect(header).toHaveAttribute('scope', 'col'))
    expect(headers[4]).toHaveAttribute('aria-sort', 'descending')
    expect(headers[0]).not.toHaveAttribute('aria-sort')

    fireEvent.change(screen.getByLabelText('排序方式'), { target: { value: 'name' } })
    expect(headers[0]).toHaveAttribute('aria-sort', 'ascending')
    expect(headers[4]).not.toHaveAttribute('aria-sort')

    expect(screen.getByRole('button', { name: '待处理 2' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: '全部 2' })).toHaveAttribute('aria-pressed', 'false')
  })

  it('compact 模式不显示表头、搜索、排序与分页，空列表也有空态', () => {
    const { rerender } = render(<Candidates items={Array.from({ length: 8 }, (_, index) => makeCandidate(index + 1))} openCandidate={() => {}} compact />)
    expect(screen.queryByRole('heading', { name: '候选人关系' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('搜索候选人')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('排序方式')).not.toBeInTheDocument()
    expect(screen.getAllByRole('row')).toHaveLength(9)
    expect(screen.queryByText(/第 \d+ \/ \d+ 页/)).not.toBeInTheDocument()

    rerender(<Candidates items={[]} openCandidate={() => {}} compact />)
    expect(screen.getByText('没有符合当前条件的候选人。')).toBeInTheDocument()
  })
})
