import type { Workflow } from './workflowModel'

export type ResolvedWorkflow = { id: string; value: Workflow }

export async function resolveWorkflowRevision(
  workflowId: string,
  load: (id: string) => Promise<Workflow>,
  maxHops = 8,
): Promise<ResolvedWorkflow> {
  let currentId = workflowId
  let value = await load(currentId)
  const visited = new Set([currentId])

  for (let hop = 0; hop < maxHops && value.workflow.status === 'superseded'; hop += 1) {
    const latestId = value.latest_revision_workflow_id?.trim()
    const directId = value.superseded_by_workflow_id?.trim()
    const nextId = latestId && latestId !== currentId ? latestId : directId
    if (!nextId || visited.has(nextId)) break
    visited.add(nextId)
    currentId = nextId
    value = await load(currentId)
  }

  return { id: currentId, value }
}
