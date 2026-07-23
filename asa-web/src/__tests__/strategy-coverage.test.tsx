import { describe, expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { StrategyCoverage, parseStrategyCoverage } from '../workflows/StrategyCoverage'
import { WorkflowStrategy } from '../workflows/WorkflowStrategy'

// S4-3c-4（N6）：策略全要素消费检查展示 —— 已消费折叠计数 + 未使用显式清单；
// 旧策略/无原型岗位 coverage_report=null 不渲染。

const coverageReport = {
  version: 'n6_coverage_v1',
  archetype_id: 'tme_computing_power',
  consumed: ['T1 竞对原厂', 'T3 相邻池（未确认）', '排除规则：行业不放宽：排除非半导体/非电源背景', '关键词组 competitor_tme'],
  unused: [
    { element: 'T2 客户整机厂', reason: '种子T2 客户整机厂仅部分消费：1/11 进入 step2，缺 联宝、荣耀、浪潮' },
    { element: '杭州优先', reason: '种子地点策略「杭州优先；其他地方看人选沟通情况」未进入 strategy_v2：schema 无地点策略落点' },
  ],
  coverage_rate: 4 / 6,
  element_count: 6,
  consumed_count: 4,
}

describe('parseStrategyCoverage', () => {
  it('null/undefined/空对象一律返回 null（无报告不渲染）', () => {
    expect(parseStrategyCoverage(null)).toBeNull()
    expect(parseStrategyCoverage(undefined)).toBeNull()
    expect(parseStrategyCoverage({})).toBeNull()
    expect(parseStrategyCoverage('{"consumed":["T1"]}')).toBeNull()
    expect(parseStrategyCoverage({ consumed: [], unused: [], coverage_rate: 1 })).toBeNull()
  })

  it('解析 consumed/unused/coverage_rate，非法条目被过滤', () => {
    const parsed = parseStrategyCoverage(coverageReport)
    expect(parsed).not.toBeNull()
    expect(parsed?.consumed).toHaveLength(4)
    expect(parsed?.unused).toHaveLength(2)
    expect(parsed?.coverageRate).toBeCloseTo(4 / 6, 5)
    const messy = parseStrategyCoverage({ consumed: ['T1 竞对原厂', 7, ''], unused: [{ element: '杭州优先' }, { reason: '缺元素名' }, 'junk'], coverage_rate: 'oops' })
    expect(messy?.consumed).toEqual(['T1 竞对原厂', '7'])
    expect(messy?.unused).toEqual([{ element: '杭州优先', reason: '' }])
    expect(messy?.coverageRate).toBe(0)
  })
})

describe('策略要素消费区（StrategyCoverage）', () => {
  it('渲染覆盖率头部、已消费折叠计数与未使用显式清单', () => {
    render(<StrategyCoverage report={coverageReport} />)
    expect(screen.getByText('这些信息用上了吗')).toBeInTheDocument()
    expect(screen.getByText('已采用 4 项（占 67%） · 2 项没用上')).toBeInTheDocument()
    expect(screen.getByText('已采用 4 项（点击展开）')).toBeInTheDocument()
    // 没用上清单：要素名单标题 + 逐项原因
    expect(screen.getByText('没用上（附原因）：T2 客户整机厂、杭州优先')).toBeInTheDocument()
    expect(screen.getByText(/仅部分消费：1\/11 进入 step2/)).toBeInTheDocument()
    expect(screen.getByText(/schema 无地点策略落点/)).toBeInTheDocument()
    // 已消费折叠在 details 内，展开后可见全部要素
    const consumed = document.querySelector('.strategy-coverage-consumed') as HTMLDetailsElement
    expect(consumed).not.toBeNull()
    expect(consumed.open).toBe(false)
    consumed.open = true
    expect(within(consumed).getByText('T1 竞对原厂')).toBeInTheDocument()
    expect(within(consumed).getByText('关键词组 competitor_tme')).toBeInTheDocument()
  })

  it('全部消费时无未使用区', () => {
    render(<StrategyCoverage report={{ consumed: ['T1 竞对原厂', '杭州优先'], unused: [], coverage_rate: 1 }} />)
    expect(screen.getByText(/占 100%/)).toBeInTheDocument()
    expect(screen.queryByText(/^没用上（附原因）：/)).not.toBeInTheDocument()
  })

  it('coverage_report 为 null（旧策略/无原型）时不渲染任何内容', () => {
    const { container } = render(<StrategyCoverage report={null} />)
    expect(container.firstChild).toBeNull()
  })
})

describe('工作流策略区接线（WorkflowStrategy + coverage）', () => {
  const strategy = { generation: { mode: 'llm', model: 'ASA Model' } }
  const channels = { liepin: [{ query: 'MPS TME DrMOS', purpose: 'T1 友商' }], xsaas: [{ query: '矽力杰 技术市场', purpose: 'T1 友商' }] }

  it('传入 coverage_report 时策略区内出现消费检查区', () => {
    render(<WorkflowStrategy strategy={strategy} channels={channels} gates={{}} coverage={coverageReport} open={false} toggle={() => undefined} />)
    const section = document.querySelector('.workflow-strategy') as HTMLElement
    expect(within(section).getByText('这些信息用上了吗')).toBeInTheDocument()
    expect(within(section).getByText('没用上（附原因）：T2 客户整机厂、杭州优先')).toBeInTheDocument()
  })

  it('不传 coverage（旧策略/无原型）时策略区照常渲染且无消费检查区', () => {
    render(<WorkflowStrategy strategy={strategy} channels={channels} gates={{}} open={false} toggle={() => undefined} />)
    const section = document.querySelector('.workflow-strategy') as HTMLElement
    expect(within(section).getByText('多渠道寻访策略')).toBeInTheDocument()
    expect(within(section).queryByText('这些信息用上了吗')).not.toBeInTheDocument()
  })
})
