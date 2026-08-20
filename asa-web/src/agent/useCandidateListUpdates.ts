import { useEffect, useRef } from 'react'
import type { CandidateListCardData } from '../workflows/CandidateListCard'
import { updateCandidateListDialogData, type CandidateChange } from './candidateListDialogUpdate'

type CandidateListUpdate = Pick<CandidateChange, 'id' | 'stage' | 'isStopped'> & {
  updated_at?: string
  job_candidate_id?: number
  is_stopped?: boolean
}

const POLL_INTERVAL_MS = 2500
// 批量连续变更合并窗口：窗口内到达的变更累积后一次性应用，
// 避免批量操作期间每 2.5s 连锁重建名单导致的视觉跳动。
const FLUSH_DELAY_MS = 800

/**
 * 名单弹窗打开时轮询服务端候选人变更，跨窗口/跨 webview 同步列表状态。
 * 止跳要点：
 * - effect 只依赖 jobId：onUpdate/data 每次渲染都是新引用，用 ref 持有，
 *   应用更新产生的新 data 不再重启轮询（旧实现因此连锁跳动）；
 * - 变更先入缓冲，800ms 合并窗口到点后一批一次应用；
 * - 子集卡（subset=true）同样轮询，但 updateCandidateListDialogData 只做
 *   原位标签/计数更新，不重构分组、不移组。
 */
export function useCandidateListUpdates(
  data: CandidateListCardData | null,
  onUpdate: (updater: (prev: CandidateListCardData) => CandidateListCardData) => void,
) {
  const rawJobId = data?.context?.type === 'job' ? Number(data.context.id) : 0
  const jobId = Number.isFinite(rawJobId) && rawJobId ? rawJobId : 0
  const onUpdateRef = useRef(onUpdate)
  useEffect(() => {
    onUpdateRef.current = onUpdate
  }, [onUpdate])
  const sinceRef = useRef('')
  const pendingRef = useRef<CandidateChange[]>([])
  const flushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (!jobId) return undefined
    sinceRef.current = ''
    pendingRef.current = []
    let mounted = true

    const flush = () => {
      flushTimerRef.current = null
      const batch = pendingRef.current
      pendingRef.current = []
      if (!batch.length || !mounted) return
      onUpdateRef.current(prev => batch.reduce((acc, change) => updateCandidateListDialogData(acc, change), prev))
    }
    const scheduleFlush = () => {
      if (flushTimerRef.current) return
      flushTimerRef.current = setTimeout(flush, FLUSH_DELAY_MS)
    }

    const poll = async () => {
      try {
        const sinceParam = sinceRef.current ? `&since=${encodeURIComponent(sinceRef.current)}` : ''
        const res = await fetch(`/api/asa/floating/candidate-updates?job_id=${jobId}${sinceParam}`)
        if (!res.ok) return
        const result = (await res.json()) as { changes?: CandidateListUpdate[] }
        // since 无论是否有有效变更都推进，避免同批变更重复投递。
        const newest = (result.changes || []).map(change => change.updated_at).filter(Boolean).sort().pop()
        if (newest) sinceRef.current = newest
        const changes = (result.changes || [])
          .map((change): CandidateChange => ({
            id: Number(change.id ?? change.job_candidate_id),
            stage: change.stage,
            isStopped: change.is_stopped ?? change.isStopped,
          }))
          .filter(change => Number.isFinite(change.id))
        if (!changes.length || !mounted) return
        pendingRef.current.push(...changes)
        scheduleFlush()
      } catch {
        // 轮询失败静默，下次继续
      }
    }

    poll()
    const timer = setInterval(poll, POLL_INTERVAL_MS)
    return () => {
      mounted = false
      clearInterval(timer)
      if (flushTimerRef.current) {
        clearTimeout(flushTimerRef.current)
        flushTimerRef.current = null
      }
      pendingRef.current = []
    }
  }, [jobId])
}
