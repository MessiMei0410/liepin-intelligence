import { useEffect, useRef, useState } from 'react'

// agent 域事件（GET /api/v1/events，text/event-stream，命名事件 event: workflow）。
export type AgentEvent = {
  id: number
  workflow_id?: string
  step_id?: number | null
  event_type?: string
  status?: string
  summary?: string
}

// SSE 增量通道（R7）。
// Core 当前仅支持 workflow_id 过滤、不支持 after 游标：每次（重）连都从该工作流首条事件
// 重放，因此用 minEventId（面板已加载详情里的最大事件 id）做客户端水位去重，只把水位之后
// 的新事件派发给 onEvent；心跳帧（: heartbeat）不携带 data，EventSource 不会触发回调。
// 页面隐藏时主动断开（与既有轮询的 document.hidden 策略一致），可见时重连；断线由
// EventSource 自动重连，返回值仅反映“当前是否打开”，调用方据此降级轮询频率。
export const useWorkflowEventStream = (
  workflowId: string | undefined,
  minEventId: number,
  onEvent: (event: AgentEvent) => void,
): boolean => {
  const [connected, setConnected] = useState(false)
  const [visible, setVisible] = useState(() => !document.hidden)
  const onEventRef = useRef(onEvent)
  const minEventIdRef = useRef(minEventId)

  useEffect(() => { onEventRef.current = onEvent })
  useEffect(() => { minEventIdRef.current = Math.max(minEventIdRef.current, minEventId) }, [minEventId])

  useEffect(() => {
    const onVisibility = () => setVisible(!document.hidden)
    document.addEventListener('visibilitychange', onVisibility)
    return () => document.removeEventListener('visibilitychange', onVisibility)
  }, [])

  useEffect(() => {
    if (!workflowId || !visible || typeof EventSource === 'undefined') return
    const source = new EventSource(`/api/v1/events?workflow_id=${encodeURIComponent(workflowId)}`)
    source.onopen = () => setConnected(true)
    source.onerror = () => setConnected(false)
    source.addEventListener('workflow', message => {
      const frame = message as MessageEvent<string>
      const id = Number(frame.lastEventId || 0)
      if (id && id <= minEventIdRef.current) return
      if (id) minEventIdRef.current = id
      try {
        onEventRef.current(JSON.parse(frame.data) as AgentEvent)
      } catch { /* 无法解析的帧直接忽略，轮询兜底。 */ }
    })
    return () => {
      source.close()
      setConnected(false)
    }
  }, [workflowId, visible])

  return connected
}
