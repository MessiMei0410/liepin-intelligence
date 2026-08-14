import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SourcingTrace } from '../panels/SourcingTrace'
import { candidateDetail } from './helpers'

describe('candidate sourcing lineage', () => {
  it('shows Mapping task and candidate index without inventing channel query', () => {
    render(<SourcingTrace value={{
      ...candidateDetail,
      source_links: [{ source_system: 'mapping', source_entity_type: 'external_profile', source_entity_id: 'u1', source_url: 'https://example.com/profile/1' }],
      source_lineage: [{ source_type: 'mapping', workflow_id: 'wf-map-1', artifact_id: 'mapping_task_1', artifact_title: 'Mapping 直挖任务卡', candidate_index: 2 }],
    }} />)

    expect(screen.getByText(/任务卡 mapping_task_1/)).toBeInTheDocument()
    expect(screen.getByText(/候选 3/)).toBeInTheDocument()
    expect(screen.getByText(/工作流 wf-map-1/)).toBeInTheDocument()
    expect(screen.getByText(/独立扩圈来源/)).toBeInTheDocument()
    expect(screen.queryByText(/关键词：/)).not.toBeInTheDocument()
  })
})
