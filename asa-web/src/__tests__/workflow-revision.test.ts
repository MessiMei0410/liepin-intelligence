import { describe, expect, it, vi } from 'vitest'
import type { Workflow } from '../api'
import { resolveWorkflowRevision } from '../workflow/workflowRevision'
import { plannedWorkflow } from './helpers'

const workflow = (id: string, status: string, latest?: string, direct?: string): Workflow => ({
  ...plannedWorkflow,
  workflow: { ...plannedWorkflow.workflow, workflow_id: id, status },
  latest_revision_workflow_id: latest,
  superseded_by_workflow_id: direct,
})

describe('工作流修订链', () => {
  it('打开根工作流时直接加载最新修订版', async () => {
    const values: Record<string, Workflow> = {
      root: workflow('root', 'superseded', 'revision-4', 'revision-1'),
      'revision-4': workflow('revision-4', 'waiting_approval', 'revision-4'),
    }
    const load = vi.fn(async (id: string) => values[id])

    const resolved = await resolveWorkflowRevision('root', load)

    expect(resolved.id).toBe('revision-4')
    expect(resolved.value.workflow.status).toBe('waiting_approval')
    expect(load.mock.calls.map(([id]) => id)).toEqual(['root', 'revision-4'])
  })

  it('替代链异常成环时停止追踪', async () => {
    const values: Record<string, Workflow> = {
      a: workflow('a', 'superseded', 'b', 'b'),
      b: workflow('b', 'superseded', 'a', 'a'),
    }
    const load = vi.fn(async (id: string) => values[id])

    const resolved = await resolveWorkflowRevision('a', load)

    expect(resolved.id).toBe('b')
    expect(load).toHaveBeenCalledTimes(2)
  })
})
