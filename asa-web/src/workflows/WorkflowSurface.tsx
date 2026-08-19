import { useState } from 'react'
import type { Job, Workflow } from '../api'
import { CompactWorkflowDialog, type WorkflowDetailSection } from './CompactWorkflowDialog'
import { WorkflowSectionView } from './WorkflowSectionView'
import { WorkflowPanel } from './WorkflowPanel'

export function WorkflowSurface({ value, jobs, close, reload, openCandidate, archived }: {
  value: Workflow
  jobs: Job[]
  close: () => void
  reload: () => void | Promise<void>
  openCandidate: (id: number, navIds?: number[]) => void
  archived: () => void
}) {
  const [detail, setDetail] = useState<WorkflowDetailSection | null>(null)

  // 「完整详情」保留原完整面板；其余模块各走自己的独立二级界面。
  if (detail === 'full') return <WorkflowPanel
    value={value}
    jobs={jobs}
    close={() => setDetail(null)}
    closeAll={close}
    reload={reload}
    openCandidate={openCandidate}
    archived={archived}
  />

  if (detail) return <WorkflowSectionView
    value={value}
    jobs={jobs}
    section={detail}
    back={() => setDetail(null)}
    close={close}
    reload={reload}
    openCandidate={openCandidate}
    openFull={() => setDetail('full')}
  />

  return <CompactWorkflowDialog value={value} close={close} reload={reload} archived={archived} openDetail={setDetail} />
}
