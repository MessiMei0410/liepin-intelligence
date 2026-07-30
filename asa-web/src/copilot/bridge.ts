import { tabs } from '../shared/tabs'

const copilotModeLabel: Record<string, string> = {
  candidate_review: '人选复核',
  job_review: '岗位复核',
  workflow_review: '工作流复核',
  strategy_revision: '策略调整',
  page_review: '页面总览',
}

export const nativeCopilotBridge = () => (window as Window & { webkit?: { messageHandlers?: { asaNative?: { postMessage: (message: Record<string, unknown>) => void } } } }).webkit?.messageHandlers?.asaNative
export const copilotContextLabel = (context: Record<string, unknown>) => context.type === 'job' ? String(context.job || `岗位 #${context.id}`) : context.type === 'candidate' ? String(context.candidate || `候选人 #${context.id}`) : context.type === 'workflow' ? `工作流 ${context.id}` : tabs.find(item => item[0] === context.page)?.[1] || '总览'
export const publishCopilotContext = async (context: Record<string, unknown>, trigger: string, explicit: boolean) => {
  if (!nativeCopilotBridge()) return false
  const mode = String(context.mode || '')
  const subtitle = [context.client, context.job, copilotModeLabel[mode] || mode, tabs.find(item => item[0] === context.page)?.[1]].filter(Boolean).join(' / ') || 'ASA Agent'
  const object = context.type === 'page'
    ? null
    : { type: context.type, id: context.id, label: copilotContextLabel(context) }
  try {
    const response = await fetch('/api/asa/floating/context', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        protocol_version: 'copilot_context_v2',
        surface: 'a_system',
        instance_id: 'asa-agent',
        trigger,
        explicit,
        user_selected: explicit,
        page_focused: true,
        page_visible: true,
        view_context: { page: context.page || 'overview', object },
        context: { ...context, label: copilotContextLabel(context), subtitle },
      }),
    })
    return response.ok
  } catch {
    return false
  }
}
export const openCopilotWindow = async (context: Record<string, unknown>) => {
  const nativeBridge = nativeCopilotBridge()
  if (nativeBridge) {
    await publishCopilotContext(context, 'copilot', true)
    nativeBridge.postMessage({ type: 'showFloating' })
    return
  }
  // R12-b：浏览器环境不再经 URL 投递上下文（通道 B 已拆除），打开的 surface 页是纯转发器/只读提示。
  const params = new URLSearchParams({ surface: 'copilot' })
  const popup = window.open(`${location.pathname}?${params}`, 'asa-copilot', 'popup=yes,width=390,height=680,resizable=yes,scrollbars=no')
  popup?.focus()
}
