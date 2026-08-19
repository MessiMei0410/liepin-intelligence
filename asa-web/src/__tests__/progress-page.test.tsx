import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Progress } from '../pages/Progress'
import type { Candidate } from '../api'

const makeCandidate = (id: number, stage: string, extra: Partial<Candidate> = {}): Candidate => ({
  id,
  person_id: id,
  name: `候选人${id}`,
  client: 'ACME',
  job: '前端工程师',
  flow_bucket: stage,
  clean_stage: stage,
  updated_at: '2026-08-05T10:00:00',
  ...extra,
})

const cardButtons = () => screen.getAllByRole('button', { name: /候选人\d+/ })

describe('人选进度 Progress', () => {
  beforeEach(() => {
    // 页面筛选状态现在走 sessionStorage 持久化：用例间重置，避免互相污染。
    sessionStorage.clear()
  })

  it('按阶段分组并展示总人数与阶段数', () => {
    render(<Progress items={[makeCandidate(1, '初筛'), makeCandidate(2, '初筛'), makeCandidate(3, '复试')]} openCandidate={() => {}} />)
    expect(screen.getByRole('status')).toHaveTextContent('共 3 位人选 · 2 个阶段')
    expect(screen.getByRole('region', { name: '初筛' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '复试' })).toBeInTheDocument()
    expect(cardButtons()).toHaveLength(3)
  })

  it('单阶段超过一页时分批显示，组内按更新时间新→旧稳定排序', () => {
    const items = Array.from({ length: 12 }, (_, index) => makeCandidate(index + 1, '初筛'))
    render(<Progress items={items} openCandidate={() => {}} />)

    const primary = screen.getByRole('region', { name: '初筛' })
    const buttons = within(primary).getAllByRole('button', { name: /候选人\d+/ })
    expect(buttons).toHaveLength(10)
    // 同一更新时间下按 id 倒序兜底：第一页 12–3，第二页 2–1。
    expect(buttons[0].textContent).toContain('候选人12')
    expect(screen.getByText('第 1 / 2 页 · 显示 1–10 / 共 12 个')).toBeInTheDocument()
    expect(screen.queryByText('候选人2')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '上一页' })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(cardButtons()).toHaveLength(2)
    expect(screen.getByText('候选人2')).toBeInTheDocument()
    expect(screen.getByText('第 2 / 2 页 · 显示 11–12 / 共 12 个')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled()
  })

  it('各阶段分页独立互不影响', () => {
    const items = [
      ...Array.from({ length: 12 }, (_, index) => makeCandidate(index + 1, '初筛')),
      ...Array.from({ length: 3 }, (_, index) => makeCandidate(100 + index, '复试')),
    ]
    render(<Progress items={items} openCandidate={() => {}} />)

    const primary = screen.getByRole('region', { name: '初筛' })
    expect(within(primary).getAllByRole('button', { name: /候选人\d+/ })).toHaveLength(10)
    fireEvent.click(within(primary).getByRole('button', { name: '下一页' }))
    expect(within(primary).getByText('候选人2')).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: '复试' })).getByText('候选人101')).toBeInTheDocument()
  })

  it('点击候选人卡片打开候选人', () => {
    const openCandidate = vi.fn()
    render(<Progress items={[makeCandidate(7, '初筛')]} openCandidate={openCandidate} />)
    fireEvent.click(screen.getByRole('button', { name: /候选人7/ }))
    expect(openCandidate).toHaveBeenCalledWith(7)
  })

  it('搜索跨字段过滤并联动阶段计数', () => {
    const items = [
      makeCandidate(1, '初筛', { current_company: '字节跳动' }),
      makeCandidate(2, '初筛', { client: '腾讯', current_title: '产品经理', source_type: 'xsaas' }),
      makeCandidate(3, '复试', { job: '销售总监', clean_stage: '已停止' }),
    ]
    render(<Progress items={items} openCandidate={() => {}} />)
    const input = screen.getByLabelText('搜索人选进度')

    fireEvent.change(input, { target: { value: '腾讯' } })
    expect(screen.getByRole('status')).toHaveTextContent('共 1 位人选 · 1 个阶段')
    expect(screen.getByRole('region', { name: '初筛' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '复试' })).not.toBeInTheDocument()
    expect(cardButtons()).toHaveLength(1)

    fireEvent.change(input, { target: { value: 'X-SaaS' } })
    expect(screen.getByRole('status')).toHaveTextContent('共 1 位人选 · 1 个阶段')

    fireEvent.change(input, { target: { value: '不存在的人选' } })
    expect(screen.getByRole('status')).toHaveTextContent('共 0 位人选 · 0 个阶段')
    expect(screen.getByText('没有符合当前条件的人选进度。')).toBeInTheDocument()

    fireEvent.change(input, { target: { value: '' } })
    expect(screen.getByRole('status')).toHaveTextContent('共 3 位人选 · 2 个阶段')
  })

  it('Mapping 来源可按真实渠道名称检索', () => {
    render(<Progress items={[makeCandidate(9, '待复核', { source_type: 'mapping' })]} openCandidate={() => {}} />)

    fireEvent.change(screen.getByLabelText('搜索人选进度'), { target: { value: 'Mapping 直挖' } })
    expect(screen.getByRole('status')).toHaveTextContent('共 1 位人选 · 1 个阶段')
    expect(screen.getByRole('button', { name: /候选人9/ })).toBeInTheDocument()
  })

  it('阶段顺序稳定：未停止在前、已停止在后，兜底「待复核」优先', () => {
    const items = [
      makeCandidate(1, '初筛'),
      makeCandidate(2, '已停止'),
      makeCandidate(3, '复试'),
      makeCandidate(4, '', { flow_bucket: '', clean_stage: '' }),
    ]
    render(<Progress items={items} openCandidate={() => {}} />)

    expect(screen.getAllByRole('region').map(region => region.getAttribute('aria-label'))).toEqual(['待复核', '初筛', '复试', '已停止'])
  })

  it('搜索变化回到第一页，数据收缩后页码状态被夹回', () => {
    const items = Array.from({ length: 12 }, (_, index) => makeCandidate(index + 1, '初筛'))
    const { rerender } = render(<Progress items={items} openCandidate={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(screen.getByText('候选人2')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('搜索人选进度'), { target: { value: '候选人4' } })
    expect(screen.getByRole('status')).toHaveTextContent('共 1 位人选 · 1 个阶段')
    expect(screen.getByText('候选人4')).toBeInTheDocument()
    expect(screen.queryByText('候选人12')).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('搜索人选进度'), { target: { value: '' } })
    expect(screen.getByText('候选人12')).toBeInTheDocument()
    expect(screen.queryByText('候选人2')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(screen.getByText('候选人2')).toBeInTheDocument()

    rerender(<Progress items={items.slice(0, 3)} openCandidate={() => {}} />)
    expect(screen.getByText('候选人3')).toBeInTheDocument()
    expect(screen.queryByText(/第 \d+ \/ \d+ 页/)).not.toBeInTheDocument()

    rerender(<Progress items={items} openCandidate={() => {}} />)
    expect(screen.getByText('候选人12')).toBeInTheDocument()
    expect(screen.queryByText('候选人2')).not.toBeInTheDocument()
  })

  it('卡片与搜索控件具备可访问标签', () => {
    render(<Progress items={[makeCandidate(7, '初筛')]} openCandidate={() => {}} />)
    expect(screen.getByRole('button', { name: '打开候选人 候选人7（初筛）' })).toBeInTheDocument()
    expect(screen.getByLabelText('搜索人选进度')).toHaveAttribute('type', 'search')
    expect(screen.getByRole('status')).toHaveTextContent('共 1 位人选 · 1 个阶段')
  })

  it('空列表给出可靠空态', () => {
    render(<Progress items={[]} openCandidate={() => {}} />)
    expect(screen.getByText('没有符合当前条件的人选进度。')).toBeInTheDocument()
    expect(screen.queryByText(/共 0 位人选/)).not.toBeInTheDocument()
  })

  it('首屏加载中显示真实加载态，不出现假空态', () => {
    render(<Progress items={[]} openCandidate={() => {}} loading />)
    expect(screen.getByText('正在加载人选数据，请稍候…')).toBeInTheDocument()
    expect(screen.queryByText('没有符合当前条件的人选进度。')).not.toBeInTheDocument()
  })

  it('加载中但已有数据时照常渲染数据，不切换成加载态', () => {
    render(<Progress items={[makeCandidate(1, '初筛')]} openCandidate={() => {}} loading />)
    expect(screen.getByRole('status')).toHaveTextContent('共 1 位人选 · 1 个阶段')
    expect(screen.getByRole('region', { name: '初筛' })).toBeInTheDocument()
  })
})
