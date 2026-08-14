import { useCallback, useEffect, useState } from 'react'
import { ChevronDown, ChevronRight, CircleDashed, ListChecks, LoaderCircle, ShieldAlert } from 'lucide-react'
import { api } from '../api'
import type { JobProfileInsightsPayload, JobProfileItem } from '../api'
import { date } from '../shared/format'

// S8 岗位画像区块「这个岗位实际在干什么」（UX-1 业务语言，不出现"画像模型"类技术词）。
// 数据来源：已抓取人选履历的职责事实学习；先给人看——每条支持"不对"纠正（disputed 质量闭环），
// 本期不接策略/评估消费。数据不足（<3 份履历）统一空态：履历还太少，学不出画像。

const pct = (ratio: number) => `${Math.round((ratio || 0) * 100)}%`

export function JobProfileInsights({ jobId, onChanged }: { jobId: number; onChanged?: () => void | Promise<void> }) {
  const [data, setData] = useState<JobProfileInsightsPayload | null>(null)
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [pending, setPending] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    try {
      setData(await api.jobProfileInsights(jobId))
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败')
    }
  }, [jobId])

  useEffect(() => {
// eslint-disable-next-line react-hooks/set-state-in-effect
    void load()
  }, [load])

  const toggle = (key: string) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const dispute = async (itemType: string, item: JobProfileItem) => {
    const pendingKey = `${itemType}:${item.key}`
    if (pending) return
    setPending(pendingKey)
    setNotice('')
    try {
      const result = await api.disputeJobProfileItem(jobId, { item_type: itemType, item_key: item.key, item_label: item.label })
      setData(current => current ? {
        ...current,
        duties: result.duties ?? current.duties,
        tools: result.tools ?? current.tools,
        deliverables: result.deliverables ?? current.deliverables,
        customers: result.customers ?? current.customers,
        disputed: result.disputed ?? current.disputed,
        stats: result.stats ?? current.stats,
        source_count: result.source_count ?? current.source_count,
        as_of: result.as_of ?? current.as_of,
      } : current)
      setNotice(`已记录"${item.label}"不对，这条不再参与这个岗位的学习结果`)
      void api.jobProfileInsights(jobId).then(setData).catch(() => undefined)
      void Promise.resolve().then(() => onChanged?.()).catch(() => undefined)
    } catch (err) {
      setNotice(err instanceof Error ? err.message : '记录失败，请重试')
    } finally {
      setPending('')
    }
  }

  const examples = (item: JobProfileItem) => (
    <div className="job-profile-evidence">
      {item.examples.length === 0 && <p>这条没有可展示的履历片段。</p>}
      {item.examples.map((example, index) => (
        <p key={index}><b>{example.candidate}</b><span>{example.evidence}</span></p>
      ))}
    </div>
  )

  const row = (itemType: string) => (item: JobProfileItem) => {
    const key = `${itemType}:${item.key}`
    const open = expanded.has(key)
    return (
      <div className="job-profile-item" key={key}>
        <div className="job-profile-row">
          <button className="job-profile-toggle" onClick={() => toggle(key)} aria-expanded={open} title="展开示例证据">
            {open ? <ChevronDown /> : <ChevronRight />}<span>{item.label}</span>
          </button>
          <i className="job-profile-bar"><b style={{ width: `${Math.max(5, (item.ratio || 0) * 100)}%` }} /></i>
          <strong>{pct(item.ratio)} · {item.count}人</strong>
          <button
            className="job-profile-dispute"
            disabled={pending === key}
            onClick={() => void dispute(itemType, item)}
            title="这条不符合这个岗位的实际情况？标记后不再参与学习"
          >不对</button>
        </div>
        {open && examples(item)}
      </div>
    )
  }

  if (error) {
    return <section className="job-detail-section job-profile-section"><h3>这个岗位实际在干什么</h3><div className="empty"><span>{error}</span><button className="button" onClick={() => void load()}>重新加载岗位画像</button></div></section>
  }
  if (!data) {
    return (
      <section className="job-detail-section job-profile-section">
        <h3>这个岗位实际在干什么</h3>
        <div className="empty"><LoaderCircle className="spin" />正在读取学习结果…</div>
      </section>
    )
  }

  const sourceCount = Number(data.source_count || 0)
  const minCount = Number(data.min_source_count || 3)
  const disputed = data.disputed || []
  if (data.status !== 'ready') {
    return (
      <section className="job-detail-section job-profile-section">
        <h3>这个岗位实际在干什么</h3>
        <div className="empty">
          <CircleDashed />
          <span>履历还太少，学不出画像（已学习 {sourceCount} 份人选履历，至少 {minCount} 份才能学出画像）</span>
        </div>
        {disputed.length > 0 && <p className="job-profile-notice">顾问已标记 {disputed.length} 条不对，不再参与学习。</p>}
      </section>
    )
  }

  const duties = data.duties || []
  const tools = data.tools || []
  const deliverables = data.deliverables || []
  const customers = data.customers || []
  return (
    <section className="job-detail-section job-profile-section">
      <h3>这个岗位实际在干什么</h3>
      <p className="job-profile-meta">
        <ListChecks />来源 {sourceCount} 份人选履历 · 更新于 {date(data.as_of)} · 只辅助顾问判断，不接策略与评估
      </p>
      {notice && <p className="job-profile-notice">{notice}</p>}
      {duties.length > 0 && (
        <div className="job-profile-block">
          <h4>职责分布</h4>
          <div className="job-profile-list">{duties.slice(0, 12).map(row('duty'))}</div>
        </div>
      )}
      {tools.length > 0 && (
        <div className="job-profile-block">
          <h4>常用工具栈</h4>
          <div className="job-keywords">
            {tools.slice(0, 16).map(item => {
              const key = `tool:${item.key}`
              const open = expanded.has(key)
              return (
                <div key={key} className="job-profile-tool">
                  <button className="job-profile-toggle" onClick={() => toggle(key)} aria-expanded={open} title="展开示例证据">
                    {open ? <ChevronDown /> : <ChevronRight />}{item.label} ×{item.count}
                  </button>
                  <button
                    className="job-profile-dispute"
                    disabled={pending === key}
                    onClick={() => void dispute('tool', item)}
                    title="这个工具不符合实际？标记后不再参与学习"
                  >不对</button>
                  {open && examples(item)}
                </div>
              )
            })}
          </div>
        </div>
      )}
      {deliverables.length > 0 && (
        <div className="job-profile-block">
          <h4>典型产出</h4>
          <div className="job-profile-list">{deliverables.slice(0, 10).map(row('deliverable'))}</div>
        </div>
      )}
      {customers.length > 0 && (
        <div className="job-profile-block">
          <h4>面向客户与场景</h4>
          <div className="job-profile-list">{customers.slice(0, 8).map(row('customer'))}</div>
        </div>
      )}
      {disputed.length > 0 && (
        <div className="job-profile-block job-profile-disputed">
          <h4><ShieldAlert />顾问已标记不对（{disputed.length} 条，不再参与学习）</h4>
          <div className="job-keywords">
            {disputed.slice(0, 12).map(item => <span key={`${item.item_type}:${item.key}`}>{item.label} ×{item.count}</span>)}
          </div>
        </div>
      )}
    </section>
  )
}
