import { useEffect, useState } from 'react'
import { api } from '../api'
import { nativeBridge } from '../shared/nativeBridge'
import { CandidateListDialog } from './CandidateListDialog'
import type { CandidateListCardData } from '../workflows/CandidateListCard'

/**
 * 独立名单窗口（#candidate_list=1&bare=1）：名单数据由 macOS 宿主在页面加载后
 * 注入 window.__DETACHED_LIST__，这里轮询取出后用同一个 CandidateListDialog
 * 组件渲染，保证独立窗口与应用内名单 UI 完全一致。
 * 点人选通过原生桥弹出独立的详情窗口（名单窗口保持开着）；刷新走岗位名单刷新接口。
 */
export function BareCandidateList({ onOpenCandidate }: { onOpenCandidate: (id: number) => void }) {
  const [data, setData] = useState<CandidateListCardData | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  useEffect(() => {
    const timer = window.setInterval(() => {
      const payload = (window as unknown as { __DETACHED_LIST__?: CandidateListCardData }).__DETACHED_LIST__
      if (payload) {
        window.clearInterval(timer)
        setData(payload)
      }
    }, 200)
    return () => window.clearInterval(timer)
  }, [])

  if (!data) return <div className="bare-list-loading" role="status">名单加载中…</div>

  const jobId = data.context?.type === 'job' ? Number(data.context.id) : 0
  const nameOf = (id: number) => {
    for (const group of data.groups || []) {
      const hit = (group.candidates || []).find(candidate => candidate.id === id)
      if (hit) return hit.name
    }
    return ''
  }
  // 点人选：弹独立详情窗口；无原生桥（纯浏览器调试）时退回同窗口导航。
  const openDetail = (id: number) => {
    if (nativeBridge('openDetachedDialog', { title: nameOf(id) || '候选人详情', url: `/asa-app#candidate=${id}&bare=1` })) return
    onOpenCandidate(id)
  }
  const refresh = async () => {
    if (!jobId || refreshing) return
    setRefreshing(true)
    try {
      const bonder = Array.isArray(data.groups) && data.groups.some(group => group.key === 'bonder')
      const result = await api.candidateListRefresh(jobId, bonder, data.filter_mode)
      setData(result.card)
    } catch {
      // 刷新失败保留当前名单，用户可再点一次。
    } finally {
      setRefreshing(false)
    }
  }

  return (
    <CandidateListDialog
      data={data}
      onOpenCandidate={openDetail}
      onClose={() => window.history.back()}
      onRefresh={jobId ? () => void refresh() : undefined}
      refreshing={refreshing}
    />
  )
}
