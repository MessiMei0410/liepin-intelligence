import { arrayValue, recordValue } from '../shared/records'

// S4-3c-4（N6）：策略全要素消费检查展示 —— 种子要素哪些进了 strategy_v2、哪些被漏用。
// 数据来自 strategy_v2.coverage_report（后端 strategy_v2.build_coverage_report 产出）；
// 旧策略/无原型岗位 coverage_report=null，整区不渲染。

export interface CoverageUnusedItem {
  element: string
  reason: string
}

export interface StrategyCoverageReport {
  consumed: string[]
  unused: CoverageUnusedItem[]
  coverageRate: number
}

export function parseStrategyCoverage(value: unknown): StrategyCoverageReport | null {
  const record = recordValue(value)
  const consumed = arrayValue(record.consumed).map(item => String(item || '')).filter(Boolean)
  const unused = arrayValue(record.unused)
    .map(item => {
      const entry = recordValue(item)
      return { element: String(entry.element || ''), reason: String(entry.reason || '') }
    })
    .filter(item => item.element)
  if (!consumed.length && !unused.length) return null
  const rate = Number(record.coverage_rate)
  return { consumed, unused, coverageRate: Number.isFinite(rate) ? rate : 0 }
}

export function StrategyCoverage({ report }: { report: unknown }) {
  const parsed = parseStrategyCoverage(report)
  if (!parsed) return null
  const percent = Math.round(parsed.coverageRate * 100)
  return <div className="strategy-coverage">
    <div className="strategy-coverage-head">
      <b>这些信息用上了吗</b>
      <span>已采用 {parsed.consumed.length} 项（占 {percent}%）{parsed.unused.length ? ` · ${parsed.unused.length} 项没用上` : ''}</span>
    </div>
    {parsed.consumed.length > 0 && <details className="strategy-coverage-consumed">
      <summary>已采用 {parsed.consumed.length} 项（点击展开）</summary>
      <div>{parsed.consumed.map(element => <span key={element}>{element}</span>)}</div>
    </details>}
    {parsed.unused.length > 0 && <div className="strategy-coverage-unused">
      <b>没用上（附原因）：{parsed.unused.map(item => item.element).join('、')}</b>
      <ul>{parsed.unused.map(item => <li key={item.element}><b>{item.element}</b><span>{item.reason}</span></li>)}</ul>
    </div>}
  </div>
}
