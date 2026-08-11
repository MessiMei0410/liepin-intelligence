import { useState } from 'react'
import {
  ChevronLeft, CircleDashed, FileText, Workflow as WorkflowIcon, X,
} from 'lucide-react'
import type { Job, Workflow } from '../api'
import { DialogPanel } from '../shared/Dialog'
import { date } from '../shared/format'
import { recordValue } from '../shared/records'
import { SectionHead } from '../shared/primitives'
import { mapWorkflowStatus } from '../workflow/statusMapping'
import { useWorkflowLiveSync } from './useWorkflowLiveSync'
import { detailItems, type WorkflowDetailSection } from './CompactWorkflowDialog'
import { humanizeWorkflowEvent, stepTone } from './utils'
import { WorkflowTarget } from './WorkflowTarget'
import { WorkflowStrategy } from './WorkflowStrategy'
import { WorkflowCandidates } from './WorkflowCandidates'
import { WorkflowFunnel } from './WorkflowFunnel'
import { WorkflowArtifactDialog } from './WorkflowArtifactDialog'
import { artifactAbsenceMessage, artifactStatusLabel, artifactTypeLabel } from './artifactPresentation'

export type WorkflowSection = Exclude<WorkflowDetailSection, 'full'>

// 「查看」菜单的模块二级界面：每个模块（策略/人选/漏斗/动态/产物）一个独立界面，
// 只挂载对应模块并发起它自己的按需请求；返回键回到轻量步骤摘要，完整详情仍走 WorkflowPanel。
export function WorkflowSectionView({ value, jobs, section, back, close, reload, openCandidate, openFull }: {
  value: Workflow
  jobs: Job[]
  section: WorkflowSection
  back: () => void
  close: () => void
  reload: () => void | Promise<void>
  openCandidate: (id: number) => void
  openFull: () => void
}) {
  const [strategyOpen, setStrategyOpen] = useState(true)
  const [artifactCardId, setArtifactCardId] = useState('')
  // SSE + 摘要轮询在后台刷新数据，本界面不渲染耗时，无需使用时钟返回值。
  useWorkflowLiveSync(value, reload)
  const status = value.workflow.status
  const businessOutcome = value.business_outcome ?? value.workflow.business_outcome ?? value.goal.business_outcome
  const mapped = mapWorkflowStatus({ status, business_outcome: businessOutcome, steps: value.steps })
  const meta = detailItems.find(item => item.id === section) || detailItems[0]

  // 与 WorkflowPanel 同源的数据推导：策略/寻访/评估步骤的输出解析，保证两个界面口径一致。
  const strategyStep = value.steps.find(step => step.capability_id === 'search_strategy')
  const strategy = recordValue(recordValue(strategyStep?.output).strategy)
  const strategyChannels = recordValue(strategy.channels)
  const reviewGates = recordValue(strategy.review_gates)
  const strategyV2 = recordValue(recordValue(strategyStep?.output).strategy_v2)
  const strategyCoverage = strategyV2.coverage_report
  const sourcingStep = value.steps.find(step => step.capability_id === 'multi_channel_sourcing')
  const strategyEditable = ['pending', 'waiting_approval', 'blocked', 'failed'].includes(sourcingStep?.status || '')
  const externalResult = recordValue(recordValue(sourcingStep?.output).external_result)
  const appliedResult = recordValue(recordValue(externalResult.intake).applied)
  const assessmentStep = value.steps.find(step => step.capability_id === 'candidate_batch_assessment')
  const assessmentQueue = recordValue(recordValue(assessmentStep?.output).assessment_queue)
  const assessmentResultSummary = String(recordValue(assessmentStep?.output).summary || '').trim()
  const hasAssessmentResult = assessmentStep?.status === 'completed' && (Object.keys(assessmentQueue).length > 0 || !!assessmentResultSummary)
  const contextJobId = Number(value.goal.context?.id || strategy.job_id || 0)
  const jobEntity = jobs.find(job => job.id === contextJobId)
  const target = {
    client: String(strategy.client || appliedResult.client || jobEntity?.client || '客户待确认'),
    job: String(strategy.job || appliedResult.job || jobEntity?.title || '岗位待确认'),
    location: jobEntity?.location || '',
    status: jobEntity?.status || '',
    priority: jobEntity?.priority || '',
    id: contextJobId,
  }
  const events = value.events || []
  const completed = value.progress?.completed ?? value.steps.filter(step => ['completed', 'skipped'].includes(step.status)).length
  const total = value.progress?.total ?? value.steps.length
  const percent = Math.max(0, Math.min(100, Math.round((value.progress?.ratio ?? completed / Math.max(1, total)) * 100)))

  return <DialogPanel panelClassName="compact-workflow-dialog workflow-section-dialog" ariaLabel={`${meta.label}：${value.goal.title}`} onEscape={back} minWidth={320} minHeight={300}>
    <header className="compact-workflow-head">
      <button className="icon-btn" onClick={back} title="返回步骤摘要" aria-label="返回"><ChevronLeft /></button>
      <div>
        <h2>{meta.label}</h2>
        <p>{value.goal.title} · {mapped.label}</p>
      </div>
      <button className="icon-btn" onClick={close} title="关闭" aria-label="关闭"><X /></button>
    </header>

    <div className="compact-workflow-body workflow-section-body">
      {section === 'strategy' && <>
        <WorkflowTarget target={target} objective={value.goal.objective} />
        <WorkflowStrategy
          strategy={strategy}
          channels={strategyChannels}
          gates={reviewGates}
          coverage={strategyCoverage}
          open={strategyOpen}
          toggle={() => setStrategyOpen(open => !open)}
          strategyV2={strategyV2}
          workflowId={value.workflow.workflow_id}
          editable={strategyEditable}
          onEdited={reload}
        />
      </>}

      {section === 'candidates' && <WorkflowCandidates
        workflowId={value.workflow.workflow_id}
        updatedAt={value.workflow.updated_at || ''}
        workflowStatus={status}
        sourcingStatus={sourcingStep?.status || 'pending'}
        assessmentQueue={assessmentQueue}
        openCandidate={openCandidate}
      />}

      {section === 'funnel' && (sourcingStep
        ? <WorkflowFunnel workflowId={value.workflow.workflow_id} updatedAt={value.workflow.updated_at || ''} />
        : <div className="workflow-section-empty"><CircleDashed /><span>该工作流没有渠道寻访步骤，暂无漏斗数据。</span></div>)}

      {section === 'events' && <div className="workflow-detail-events">
        <SectionHead title="执行动态" meta={`${events.length} 条`} />
        {events.length === 0
          ? <div className="aside-empty"><CircleDashed /><span>启动后将在这里显示过程</span></div>
          : <div className="workflow-events">{events.map(event => <div key={event.id}>
            <i className={stepTone(event.status)} />
            <span>{date(event.created_at)}</span>
            <b>{humanizeWorkflowEvent(event)}</b>
          </div>)}</div>}
      </div>}

      {section === 'artifacts' && <div className="workflow-detail-artifacts">
        <SectionHead title="结果与产物" meta={`${value.artifacts.length + (hasAssessmentResult ? 1 : 0)} 项`} />
        {hasAssessmentResult && <div className="aside-item workflow-section-assessment">
          <b>候选人核验结果</b>
          <small>{assessmentResultSummary || `本轮评估 ${Number(assessmentQueue.started || 0)} 位，岗位已评估 ${Number(assessmentQueue.completed || 0)} 位`}</small>
        </div>}
        {value.artifacts.length === 0 && !hasAssessmentResult && <div className="aside-empty artifact-empty"><CircleDashed /><span>{artifactAbsenceMessage(value)}</span></div>}
        {value.artifacts.map(artifact => <button
          type="button"
          className="aside-item artifact-item"
          key={artifact.artifact_id}
          onClick={() => setArtifactCardId(artifact.artifact_id)}
          aria-label={`查看产物：${artifact.title}`}
        >
          <FileText />
          <span><b>{artifact.title}</b><small>{artifactTypeLabel(artifact.artifact_type)} · {artifactStatusLabel(artifact.validation_status)}</small></span>
        </button>)}
      </div>}
    </div>

    <footer className="compact-workflow-foot">
      <div className="compact-progress" aria-label={`工作流进度：${completed}/${total} 步`}>
        <b>{completed}/{total} 步</b>
        <span>{mapped.label}</span>
        <strong>{percent}%</strong>
      </div>
      <div className="compact-workflow-controls">
        <button className="button" onClick={openFull}><WorkflowIcon />查看完整详情</button>
      </div>
    </footer>

    {artifactCardId && <WorkflowArtifactDialog key={artifactCardId} artifactId={artifactCardId} onClose={() => setArtifactCardId('')} />}
  </DialogPanel>
}
