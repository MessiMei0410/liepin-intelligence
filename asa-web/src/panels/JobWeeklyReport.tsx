import { useCallback, useEffect, useState } from 'react'
import { CircleDashed, FileText, LoaderCircle, RefreshCw, ScrollText, TriangleAlert } from 'lucide-react'
import { api } from '../api'
import type { JobWeeklyReportBrief, JobWeeklyReportCreateResult, JobWeeklyReportListPayload } from '../api'
import { copilotText } from '../shared/text'
import { WorkflowArtifactDialog } from '../workflows/WorkflowArtifactDialog'

// 岗位周报区块（三期驾驶舱缺口）：生成按钮 + 最新周报摘要 + 历史版本列表。
// 完整报告复用 WorkflowArtifactDialog（markdown 正文 + 下载链路），不在此另做渲染。
const countText = (value: number | null | undefined) => value === null || value === undefined ? '—' : String(value)

const briefFromCreateResult = (result: JobWeeklyReportCreateResult): JobWeeklyReportBrief | null => {
  const report = result.report
  const artifactId = String(result.artifact_id || '')
  const weekStart = String(result.week_start || report?.week_start || '')
  const weekEnd = String(result.week_end || report?.week_end || '')
  if (!report || !artifactId || !weekStart || !weekEnd) return null
  const version = Number(result.version || report.version || 1)
  const current = report.funnel?.current
  return {
    artifact_id: artifactId,
    title: ['岗位周报', report.client, report.job_title, weekStart, `v${version}`].filter(Boolean).join(' '),
    version,
    week_start: weekStart,
    week_end: weekEnd,
    generated_at: report.generated_at,
    summary: {
      total: current?.total,
      active: current?.active,
      recommended: current?.recommended,
      confirmed_this_week: report.recommendations?.confirmed_this_week,
      comparison: report.funnel?.comparison,
      risk_count: Array.isArray(report.risks) ? report.risks.length : undefined,
      suggestion_count: Array.isArray(report.suggestions) ? report.suggestions.length : undefined,
    },
  }
}

const mergeGeneratedReport = (payload: JobWeeklyReportListPayload, generated: JobWeeklyReportBrief) => {
  const same = payload.items.find(item => item.artifact_id === generated.artifact_id)
  if (same && same.version >= generated.version) return payload
  const items = [generated, ...payload.items.filter(item => item.artifact_id !== generated.artifact_id)]
  return { ...payload, latest: items[0], items }
}

function ReportRow({ item, latest, onOpen }: { item: JobWeeklyReportBrief; latest: boolean; onOpen: (artifactId: string) => void }) {
  return (
    <button type="button" className="job-weekly-report-item" onClick={() => onOpen(item.artifact_id)} aria-label={`查看岗位周报 ${item.title}`}>
      <FileText />
      <span>
        <b>{item.week_start} ~ {item.week_end}{latest ? ' · 最新' : ''}</b>
        <small>v{item.version}{item.generated_at ? ` · 生成于 ${item.generated_at}` : ''}</small>
      </span>
    </button>
  )
}

export function JobWeeklyReport({ jobId }: { jobId: number }) {
  const [data, setData] = useState<JobWeeklyReportListPayload | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [generating, setGenerating] = useState(false)
  const [generateError, setGenerateError] = useState('')
  const [generateNotice, setGenerateNotice] = useState('')
  const [openArtifactId, setOpenArtifactId] = useState('')
  const load = useCallback(async () => {
    setLoading(true)
    try {
      setData(await api.jobWeeklyReports(jobId))
      setError('')
    } catch (reason) {
      setError(copilotText(reason) || '岗位周报读取失败')
    } finally {
      setLoading(false)
    }
  }, [jobId])
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { void load() }, [load])

  const generate = async () => {
    setGenerating(true)
    setGenerateError('')
    setGenerateNotice('')
    try {
      const result = await api.generateJobWeeklyReport(jobId)
      const generated = briefFromCreateResult(result)
      if (generated) {
        setData(current => mergeGeneratedReport(current || { ok: true, job_id: jobId, latest: null, items: [] }, generated))
        setError('')
      }
      setGenerateNotice(result.receipt?.idempotent_replay
        ? `本周周报此前已生成，已返回保存版本 v${result.version || generated?.version || 1}。`
        : `本周周报已生成并保存为 v${result.version || generated?.version || 1}。`)
      void api.jobWeeklyReports(jobId).then(payload => {
        setData(generated ? mergeGeneratedReport(payload, generated) : payload)
        setError('')
      }).catch(() => undefined)
    } catch (reason) {
      setGenerateError(copilotText(reason) || '周报生成失败，请重试')
    } finally {
      setGenerating(false)
    }
  }

  const items = data && Array.isArray(data.items) ? data.items : []
  const latest = data?.latest && items.length ? items[0] : null
  const summary = latest?.summary
  return (
    <section className="job-detail-section job-weekly-report" aria-label="岗位周报">
      <h3>
        岗位周报{items.length > 0 && <span className="job-section-count">{items.length} 期</span>}
        <button type="button" className="button job-weekly-report-generate" disabled={generating} onClick={() => void generate()}>
          {generating ? <LoaderCircle className="spin" /> : <ScrollText />}{generating ? '正在生成…' : '生成本周周报'}
        </button>
      </h3>
      {generateError && <p className="job-weekly-report-message error" role="alert"><TriangleAlert />{generateError}</p>}
      {generateNotice && <p className="job-weekly-report-message" role="status"><FileText />{generateNotice}</p>}
      {loading && !data && <div className="job-weekly-report-message" role="status"><LoaderCircle className="spin" /><span>正在读取岗位周报…</span></div>}
      {error && <div className="job-weekly-report-message error" role="alert"><CircleDashed /><span>{error}</span><button type="button" className="button" onClick={() => void load()}><RefreshCw />重试</button></div>}
      {data && !error && items.length === 0 && (
        <div className="empty">还没有岗位周报。点击「生成本周周报」生成第一期（汇总漏斗、有效推荐、渠道质量、风险与建议）。</div>
      )}
      {latest && (
        <div className="job-weekly-report-latest">
          <div><span>全部人选</span><b>{countText(summary?.total)}</b></div>
          <div><span>活跃推进</span><b>{countText(summary?.active)}</b></div>
          <div><span>已推荐</span><b>{countText(summary?.recommended)}</b></div>
          <div><span>本周确认推荐</span><b>{countText(summary?.confirmed_this_week)}</b></div>
          <div><span>风险 / 建议</span><b>{countText(summary?.risk_count)} / {countText(summary?.suggestion_count)}</b></div>
          <button type="button" className="button primary" onClick={() => setOpenArtifactId(latest.artifact_id)}>查看完整报告</button>
        </div>
      )}
      {items.length > 0 && (
        <div className="job-weekly-report-list" aria-label="历史周报">
          {items.map((item, index) => <ReportRow key={item.artifact_id} item={item} latest={index === 0} onOpen={setOpenArtifactId} />)}
        </div>
      )}
      {openArtifactId && <WorkflowArtifactDialog key={openArtifactId} artifactId={openArtifactId} onClose={() => setOpenArtifactId('')} />}
    </section>
  )
}
