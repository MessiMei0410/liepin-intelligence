import { useEffect, useRef } from 'react'
import type { CandidateListCardData } from '../workflows/CandidateListCard'
import { updateCandidateListDialogData, type CandidateChange } from './candidateListDialogUpdate'

type CandidateListUpdate = Pick<CandidateChange, 'id' | 'stage' | 'isStopped'> & {
  updated_at?: string
  job_candidate_id?: number
  is_stopped?: boolean
}

/** 名单弹窗打开时轮询服务端候选人变更，跨窗口/跨 webview 同步列表状态。 */
export function useCandidateListUpdates(
  data: CandidateListCardData | null,
  onUpdate: (updater: (prev: CandidateListCardData) => CandidateListCardData) => void,
) {
  const jobIdRef = useRef<number | null>(null)
  const sinceRef = useRef<string>('')

  useEffect(() => {
    if (!data) {
      jobIdRef.current = null
      return undefined
    }
    const jobId = data.context?.type === 'job' ? Number(data.context.id) : 0
    if (!jobId || !Number.isFinite(jobId)) return undefined
    if (jobIdRef.current !== jobId) {
      jobIdRef.current = jobId
      sinceRef.current = ''
    }
    let mounted = true

    const poll = async () => {
      try {
        const sinceParam = sinceRef.current ? `&since=${encodeURIComponent(sinceRef.current)}` : ''
        const res = await fetch(`/api/asa/floating/candidate-updates?job_id=${jobId}${sinceParam}`)
        if (!res.ok) return
        const result = (await res.json()) as { changes?: CandidateListUpdate[] }
        const changes = (result.changes || [])
          .map((change): CandidateChange => ({
            id: Number(change.id ?? change.job_candidate_id),
            stage: change.stage,
            isStopped: change.is_stopped ?? change.isStopped,
          }))
          .filter(change => Number.isFinite(change.id))
        if (!changes.length || !mounted) return
        onUpdate(prev => changes.reduce((acc, change) => updateCandidateListDialogData(acc, change), prev))
        const newest = (result.changes || []).map(change => change.updated_at).filter(Boolean).sort().pop()
        if (newest) sinceRef.current = newest
      } catch (_) {
        // 轮询失败静默，下次继续
      }
    }

    poll()
    const timer = setInterval(poll, 2500)
    return () => {
      mounted = false
      clearInterval(timer)
    }
  }, [data, onUpdate])
}
