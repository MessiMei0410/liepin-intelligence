import { useEffect, useState } from 'react'
import { LoaderCircle, TriangleAlert } from 'lucide-react'
import { api } from '../api'
import type { CalibrationMetricsPayload } from '../api'

// S6-4 评估校准 · 顾问点头率（挂评估区底部的简单表格，不做图表）：
// GET /api/v1/assessments/calibration/metrics，维度×客户聚合采纳/改判/否决率。
// 数据不足的分组后端如实返回 null → 前端呈现「数据不足」，不硬算百分比。
// 文案一律业务语言（采纳率 → 顾问点头率）；改判样例内容（note/机器原判）永不在此渲染。

const percent = (value: number | null, insufficient: string) =>
  value === null || value === undefined ? insufficient : `${Math.round(value * 100)}%`

export function CalibrationMetrics() {
  const [payload, setPayload] = useState<CalibrationMetricsPayload | null>(null)
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')

  useEffect(() => {
    let cancelled = false
    api
      .assessmentCalibrationMetrics()
      .then(result => {
        if (cancelled) return
        setPayload(result)
        setState('ready')
      })
      .catch(() => {
        if (!cancelled) setState('error')
      })
    return () => {
      cancelled = true
    }
  }, [])

  const labels = payload?.labels || {}
  const title = labels.title || '评估校准 · 顾问点头率'
  const insufficient = labels.insufficient || '数据不足'
  const groups = payload?.groups || []
  const totals = payload?.totals

  return (
    <section className="assessment-dim" aria-label="评估校准">
      <div className="assessment-dim-head">
        <h3>{title}</h3>
        {totals !== undefined && totals !== null && (
          <span>
            已评估 {totals.assessments} 份 · 已采纳 {totals.accepted} · 已改判 {totals.modified} · 已否决 {totals.rejected}
          </span>
        )}
      </div>
      {state === 'loading' && (
        <p className="assessment-note-line"><LoaderCircle className="spin" size={14}/> 校准数据加载中…</p>
      )}
      {state === 'error' && (
        <p className="assessment-note-line"><TriangleAlert size={14}/> 校准数据暂时加载失败，不影响评估使用。</p>
      )}
      {state === 'ready' && groups.length === 0 && (
        <p className="assessment-note-line">还没有足够的顾问动作数据；采纳或改判几份评估后，这里会按客户和维度汇总点头率。</p>
      )}
      {state === 'ready' && groups.length > 0 && (
        <>
          <table className="assessment-segments" aria-label="顾问点头率">
            <thead>
              <tr>
                <th>客户</th><th>维度</th><th>样本数</th>
                <th>{labels.acceptance_rate || '顾问点头率'}</th>
                <th>{labels.modified_rate || '改判率'}</th>
                <th>{labels.rejected_rate || '否决率'}</th>
              </tr>
            </thead>
            <tbody>
              {groups.map(group => (
                <tr key={`${group.client}-${group.dimension}`}>
                  <td>{group.client}</td>
                  <td>{group.dimension_label}</td>
                  <td>{group.total}</td>
                  <td>{percent(group.acceptance_rate, insufficient)}</td>
                  <td>{percent(group.modified_rate, insufficient)}</td>
                  <td>{percent(group.rejected_rate, insufficient)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="assessment-note-line">
            样本数不足 {payload?.min_n ?? 3} 的分组记「{insufficient}」；你的改判口径会回流用于校准后续评估，只影响判断口径，不放宽证据要求。
          </p>
        </>
      )}
    </section>
  )
}
