import { tabs } from '../shared/tabs'

export const nativeCopilotBridge = () => (window as Window & { webkit?: { messageHandlers?: { asaNative?: { postMessage: (message: Record<string, unknown>) => void } } } }).webkit?.messageHandlers?.asaNative
export const copilotContextLabel = (context: Record<string, unknown>) => context.type === 'job' ? String(context.job || `岗位 #${context.id}`) : context.type === 'candidate' ? String(context.candidate || `候选人 #${context.id}`) : context.type === 'workflow' ? `工作流 ${context.id}` : tabs.find(item => item[0] === context.page)?.[1] || '总览'
export const publishCopilotContext = async (context: Record<string, unknown>, trigger: string, explicit: boolean) => {
  if (!nativeCopilotBridge()) return false
  const subtitle = [context.client, context.job].filter(Boolean).join(' / ') || 'ASA Agent'
  try {
    const response = await fetch('/api/asa/floating/context', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ surface: 'a_system', instance_id: 'asa-agent', trigger, explicit, user_selected: explicit, page_focused: true, page_visible: true, context: { ...context, label: copilotContextLabel(context), subtitle } }),
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
  const params = new URLSearchParams({ surface: 'copilot', context: JSON.stringify(context) })
  const popup = window.open(`${location.pathname}?${params}`, 'asa-copilot', 'popup=yes,width=390,height=680,resizable=yes,scrollbars=no')
  popup?.focus()
}
