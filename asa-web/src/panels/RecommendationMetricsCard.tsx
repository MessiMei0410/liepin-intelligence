import { useCallback, useEffect, useState } from 'react'
import { CircleDashed, LoaderCircle, RefreshCw, Target } from 'lucide-react'
import { api } from '../api'
import type { RecommendationMetrics } from '../api'
import { copilotText } from '../shared/text'

const percent = (rate: number | null) => rate === null ? '数据不足' : `${Math.round(rate * 100)}%`

export function RecommendationMetricsCard({ jobId }: { jobId: number }) {
  const [data, setData] = useState<RecommendationMetrics | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const load = useCallback(async () => {
    setLoading(true)
    try {
      setData(await api.recommendationMetrics(jobId))
      setError('')
    } catch (reason) {
      setError(copilotText(reason) || '有效推荐率读取失败')
    } finally {
      setLoading(false)
    }
  }, [jobId])
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { void load() }, [load])

  if (loading) return <section className="job-recommendation-metrics" aria-label="有效推荐率"><LoaderCircle className="spin" /><span>正在读取推荐质量…</span></section>
  if (error) return <section className="job-recommendation-metrics error" aria-label="有效推荐率"><CircleDashed /><span>{error}</span><button className="button" onClick={() => void load()}><RefreshCw />重试</button></section>
  if (!data) return null
  return <section className="job-recommendation-metrics" aria-label="有效推荐率">
    <Target />
    <div><span>顾问确认可推荐</span><b>{data.confirmed_recommendations}</b></div>
    <div><span>已完成评估</span><b>{data.assessed_candidates}</b></div>
    <div><span>有效推荐率</span><b>{percent(data.rate)}</b></div>
    <small>{data.rate === null ? '完成评估并确认推荐后生成口径' : '顾问确认可推荐人数 / 已完成评估人数'}</small>
  </section>
}
