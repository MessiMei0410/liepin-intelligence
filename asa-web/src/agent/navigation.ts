import type { AgentContext } from './transport'

export const AGENT_NAVIGATE_EVENT = 'asa:open-agent'

/** 全屏对象（候选人/岗位/工作流等 overlay）关闭时广播，供名单浮窗等恢复现场。 */
export const FULL_OBJECT_CLOSED_EVENT = 'asa:full-object-closed'

export const openAgentWorkspace = (context: AgentContext) => {
  window.dispatchEvent(new CustomEvent<AgentContext>(AGENT_NAVIGATE_EVENT, { detail: context }))
}

export const notifyFullObjectClosed = () => {
  window.dispatchEvent(new CustomEvent(FULL_OBJECT_CLOSED_EVENT))
}
