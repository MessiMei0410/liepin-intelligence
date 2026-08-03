import type { AgentContext } from './transport'

export const AGENT_NAVIGATE_EVENT = 'asa:open-agent'

export const openAgentWorkspace = (context: AgentContext) => {
  window.dispatchEvent(new CustomEvent<AgentContext>(AGENT_NAVIGATE_EVENT, { detail: context }))
}
