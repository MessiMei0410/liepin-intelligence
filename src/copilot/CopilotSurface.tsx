import { Copilot } from './Copilot'

export function CopilotSurface() {
  let context: Record<string, unknown> = { type: 'page', page: 'overview' }
  try {
    const value = JSON.parse(new URLSearchParams(location.search).get('context') || '{}')
    if (value && typeof value === 'object') context = value
  } catch { /* Keep the safe default context. */ }
  const openWorkflow = async (id: string) => {
    if (window.opener && !window.opener.closed) {
      window.opener.location.hash = `workflow=${id}`
      window.opener.focus()
      return
    }
    const agentUrl = new URL(location.href)
    agentUrl.search = ''
    agentUrl.hash = `workflow=${id}`
    window.open(agentUrl, 'asa-agent')
  }
  return <main className="copilot-surface"><Copilot context={context} openWorkflow={openWorkflow} standalone /></main>
}
