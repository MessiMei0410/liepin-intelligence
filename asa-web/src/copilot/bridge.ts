import { openAgentWorkspace } from '../agent/navigation'

// Compatibility export for callers outside the React subtree. The interaction now stays
// in the main Agent workspace; no native bridge or floating context channel is used.
export const openCopilotWindow = async (context: Record<string, unknown>) => openAgentWorkspace(context)
