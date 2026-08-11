import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type Workflow } from '../api'
import { summarySignature, workflowDetailSignature } from '../workflow/workflowSummary'
import { useWorkflowEventStream } from '../workflow/useWorkflowEventStream'
import { activeWorkflowStatuses } from './utils'

// 轻量浮层与模块二级界面共用的实况同步：SSE 优先、摘要轮询兜底、每秒刷新耗时。
// 只比对 Core 摘要签名决定是否 reload，界面自身不推断或伪造执行状态。
export function useWorkflowLiveSync(value: Workflow, reload: () => void | Promise<void>) {
  const [now, setNow] = useState(0)
  const live = activeWorkflowStatuses.has(value.workflow.status)
  const signatureRef = useRef(workflowDetailSignature(value))
  const checkingRef = useRef(false)
  useEffect(() => { signatureRef.current = workflowDetailSignature(value) }, [value])
  const checkSummary = useCallback(async () => {
    if (checkingRef.current || document.hidden) return
    checkingRef.current = true
    try {
      const summary = await api.workflowSummary(value.workflow.workflow_id)
      const nextSignature = summarySignature(summary)
      if (nextSignature !== signatureRef.current) {
        await reload()
        signatureRef.current = nextSignature
      }
    } catch {
      // Core 短暂不可达：保留当前界面，下一轮再试。
    } finally {
      checkingRef.current = false
    }
  }, [reload, value.workflow.workflow_id])
  const latestEventId = (value.events || []).reduce((max, event) => Math.max(max, event.id), 0)
  const streamConnected = useWorkflowEventStream(live ? value.workflow.workflow_id : undefined, latestEventId, () => { void checkSummary() })

  useEffect(() => {
    if (!live) return undefined
    const poll = window.setInterval(() => { void checkSummary() }, streamConnected ? 15_000 : 1_200)
    const clock = window.setInterval(() => setNow(Date.now()), 1_000)
    const onVisible = () => { if (!document.hidden) void checkSummary() }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      window.clearInterval(poll)
      window.clearInterval(clock)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [checkSummary, live, streamConnected])

  return now
}
