import { AlertTriangle, ArrowLeft, Download, LoaderCircle, Minus, RefreshCw, TrendingDown, TrendingUp } from 'lucide-react'
import type { AnalysisResult, AnalysisTrend } from '../api'
import { sourceLabel } from '../shared/format'
import { candidateRecommendationLabel } from '../shared/candidateRecommendation'
import { mapWorkflowStatus } from '../workflow/statusMapping'

const columnLabels: Record<string, string> = {
  client: '客户', name: '人选', company: '当前公司', title: '职位', city: '城市',
  job: '目标岗位', job_id: '岗位 ID', clean_stage: '推进阶段', candidates: '全部人选',
  active_candidates: '有效人选', total: '全部人选', active: '有效人选', stopped: '已停止',
  contacted: '已触达', recommended: '已推荐', recommendation_rate: '推荐率',
  fit_score: '匹配分', fit_level: '匹配等级', recommendation: '推进建议',
  evidence_coverage: '证据覆盖率', channel: '渠道', queries: '查询', recalled: '召回',
  intaked: '新增入库', assessed: '已评估', high_score: '高分人选', intake_rate: '入库率',
  status: '步骤状态', issue: '质量问题', count: '数量', severity: '严重程度',
  confirmed: '确认推荐', interviewed: '进入面试', interview_rate: '面试转化',
  high_score_rate: '高分率', created_at: '创建时间', closed_at: '关闭时间',
  closure_days: '关闭周期（天）', workflow_id: '工作流', workflow_title: '工作流名称',
  review_state: '复盘状态', updated_at: '更新时间',
}


const severityLabels: Record<string, string> = { high: '高', medium: '中', low: '低' }

const valueText = (value: number | null, unit: string) => {
  if (value === null) return '数据不足'
  if (unit === 'ratio') return `${Math.round(value * 1000) / 10}%`
  if (unit === 'days') return `${new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 }).format(value)} 天`
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 }).format(value)
}

const cellText = (value: unknown, column: string) => {
  if (value === null || value === undefined || value === '') return '-'
  if (column === 'status') return mapWorkflowStatus({ status: String(value) }).label
  if (column === 'channel') return sourceLabel(String(value))
  if (column === 'recommendation') return candidateRecommendationLabel(String(value || ''))
  if (column === 'severity') return severityLabels[String(value)] || String(value)
  if (typeof value === 'number' && (column.endsWith('_rate') || column === 'evidence_coverage')) {
    return `${Math.round(value * 1000) / 10}%`
  }
  if (typeof value === 'number') return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value)
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

const deltaText = (delta: number | null, unit: string) => {
  if (delta === null) return '暂无对比'
  const prefix = delta > 0 ? '+' : ''
  return `${prefix}${valueText(delta, unit)}`
}

function TrendBars({ values }: { values: Array<number | null> }) {
  const numeric = values.filter((value): value is number => value !== null)
  const min = numeric.length ? Math.min(...numeric) : 0
  const max = numeric.length ? Math.max(...numeric) : 0
  const range = max - min
  return <div className="trend-bars" aria-hidden="true">{values.map((value, index) => {
    const height = value === null ? 4 : range ? 8 + ((value - min) / range) * 26 : 20
    return <i key={index} className={value === null ? 'empty' : ''} style={{ height }} />
  })}</div>
}

export function AnalysisWorkspace({ result, trend, busy, close, refresh, exportReport }: {
  result: AnalysisResult; busy?: 'refresh' | 'export'; close: () => void;
  trend?: AnalysisTrend; refresh: () => void; exportReport: () => void;
}) {
  const unavailable = result.status === 'failed' || result.status === 'expired'
  return <div className="analysis-workspace" role="region" aria-label="完整分析" aria-busy={!!busy}>
    <header className="workspace-head">
      <button className="icon-btn" onClick={close} title="返回" aria-label="返回"><ArrowLeft /></button>
      <div><span>完整分析</span><h2>{result.headline}</h2><p>{result.question}</p></div>
      <div className="workspace-actions">
        <button className="button" disabled={!!busy || unavailable} title={unavailable ? '分析未完成，暂不可导出' : undefined} onClick={exportReport}>{busy === 'export' ? <LoaderCircle className="spin" /> : <Download />}导出</button>
        <button className="button primary" disabled={!!busy} onClick={refresh}>{busy === 'refresh' ? <LoaderCircle className="spin" /> : <RefreshCw />}{unavailable ? '重试分析' : '刷新'}</button>
      </div>
    </header>
    {result.status === 'failed' && <section className="analysis-state error"><AlertTriangle /><div><b>分析未完成</b><span>{result.caveats[0] || '请稍后重试'}</span></div></section>}
    {result.status === 'expired' && <section className="analysis-state error"><AlertTriangle /><div><b>分析结果已过期</b><span>点击“重试分析”按相同范围生成最新结果。</span></div></section>}
    {result.status === 'partial' && <section className="analysis-state"><AlertTriangle /><div><b>分析部分完成</b><span>{result.caveats[0] || '部分数据暂时不可用，当前结果仍可查看。'}</span></div></section>}
    {!!result.metrics.length && <section className="analysis-metrics" aria-label="分析指标">
      {result.metrics.map(metric => <div key={metric.id}><span>{metric.label}</span><strong>{valueText(metric.value, metric.unit)}</strong><small>{typeof metric.sample_size === 'number' ? `样本量 ${metric.sample_size}` : metric.definition_version}</small>{metric.note && <small className="metric-note">{metric.note}</small>}</div>)}
    </section>}
    {trend && trend.run_count > 1 && <section className="analysis-trends" aria-label="变化趋势">
      <header><div><h3>变化趋势</h3><span>{trend.run_count} 次固定分析</span></div></header>
      <div>{trend.series.slice(0, 6).map(series => {
        const direction = series.delta === null || series.delta === 0 ? 'flat' : series.delta > 0 ? 'up' : 'down'
        return <article key={series.metric_id}>
          <div><span>{series.label}</span><strong>{valueText(series.latest, series.unit)}</strong></div>
          <TrendBars values={series.points.map(point => point.value)} />
          <span className={`trend-delta ${direction}`}>
            {direction === 'up' ? <TrendingUp /> : direction === 'down' ? <TrendingDown /> : <Minus />}{deltaText(series.delta, series.unit)}
          </span>
        </article>
      })}</div>
    </section>}
    {result.sections.map((section, index) => <section className="analysis-section" key={`${section.title}-${index}`}>
      <header><h3>{section.title}</h3><span>{section.rows.length} 条</span></header>
      <div className={`analysis-table ${section.type}`}>
        <table><thead><tr>{section.columns.map(column => <th key={column} scope="col">{columnLabels[column] || column}</th>)}</tr></thead>
          <tbody>{section.rows.length
            ? section.rows.map((row, rowIndex) => <tr key={rowIndex}>{section.columns.map(column => <td key={column}>{cellText(row[column], column)}</td>)}</tr>)
            : <tr><td className="analysis-empty-row" colSpan={section.columns.length}>本部分暂无数据</td></tr>}</tbody>
        </table>
      </div>
    </section>)}
    {!unavailable && !result.metrics.length && !result.sections.length && <div className="analysis-state"><AlertTriangle /><div><b>当前范围没有可展示数据</b><span>可调整分析范围后重新运行。</span></div></div>}
    {result.truncated && <div className="analysis-state"><AlertTriangle /><div><b>结果已截断</b><span>当前只展示隐私裁剪后的前 10 条结构化记录。</span></div></div>}
  </div>
}
