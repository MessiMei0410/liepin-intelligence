import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { AnalysisResult } from '../api'
import { AnalysisTemplateDialog } from '../components/AnalysisTemplateDialog'
import { AnalysisWorkspace } from '../pages/AnalysisWorkspace'

const scorecard: AnalysisResult = {
  schema_version: 'analysis_result_v1', run_id: 'analysis-scorecard', catalog_id: 'delivery_scorecard',
  catalog_version: '2026-08-03', status: 'completed', question: '交付表现如何？', scope: {},
  data_as_of: '2026-08-05T09:00:00+08:00',
  headline: '有效推荐率 50%（样本 2），推荐至面试 数据不足（样本 0），复盘完成率 33.3%（样本 3）',
  metrics: [
    { id: 'effective_recommendation_rate', label: '有效推荐率', value: 0.5, unit: 'ratio', definition_id: 'asa.effective_recommendation_rate', definition_version: '2026-08-03', sample_size: 2, note: '顾问确认推荐 ÷ 已完成评估人数（评估 is_current 且对应 run 已完成）' },
    { id: 'recommendation_to_interview_rate', label: '推荐至面试转化', value: null, unit: 'ratio', definition_id: 'asa.recommendation_to_interview_rate', definition_version: '2026-08-03', sample_size: 0, note: '确认推荐后出现面试信号 ÷ 确认推荐人数' },
    { id: 'job_closure_days_median', label: '关闭周期中位数', value: 15, unit: 'days', definition_id: 'asa.job_closure_days_median', definition_version: '2026-08-03', sample_size: 2, note: '岗位 closed_at − created_at 的天数' },
    { id: 'strategy_review_completion_rate', label: '复盘完成率', value: 0.3333, unit: 'ratio', definition_id: 'asa.strategy_review_completion_rate', definition_version: '2026-08-03', sample_size: 3, note: '有策略复盘的终局寻访工作流 ÷ 终局寻访工作流' },
  ],
  sections: [
    { type: 'table', title: '岗位推荐明细', columns: ['client', 'title', 'assessed', 'confirmed', 'recommendation_rate', 'interviewed', 'interview_rate'], rows: [{ client: '士兰微', title: '电源专家', assessed: 2, confirmed: 1, recommendation_rate: 0.5, interviewed: 0, interview_rate: null }] },
    { type: 'table', title: '寻访工作流复盘', columns: ['workflow_id', 'workflow_title', 'status', 'review_state'], rows: [{ workflow_id: 'wf1', workflow_title: '第1轮寻访', status: 'completed', review_state: '已复盘' }] },
    { type: 'table', title: '岗位关闭周期', columns: ['client', 'title', 'created_at', 'closed_at', 'closure_days'], rows: [] },
  ],
  references: [], caveats: ['无顾问确认推荐，推荐至面试转化为 null（样本量 0）。'], truncated: false,
  suggested_actions: [], supersedes_run_id: null,
}

const noop = () => {}

describe('Delivery Scorecard Workspace', () => {
  it('渲染指标当前值、样本量与中文口径说明', () => {
    render(<AnalysisWorkspace result={scorecard} close={noop} refresh={noop} exportReport={noop} />)

    expect(screen.getByText('有效推荐率')).toBeInTheDocument()
    expect(screen.getAllByText('50%').length).toBeGreaterThan(0)
    expect(screen.getAllByText('样本量 2')).toHaveLength(2)
    expect(screen.getByText('顾问确认推荐 ÷ 已完成评估人数（评估 is_current 且对应 run 已完成）')).toBeInTheDocument()
    // 天数单位与空值空态。
    expect(screen.getByText('15 天')).toBeInTheDocument()
    expect(screen.getByText('数据不足')).toBeInTheDocument()
    expect(screen.getByText('样本量 0')).toBeInTheDocument()
  })

  it('按中文化列名渲染钻取明细与空表空态', () => {
    render(<AnalysisWorkspace result={scorecard} close={noop} refresh={noop} exportReport={noop} />)

    for (const label of ['确认推荐', '进入面试', '面试转化', '工作流名称', '复盘状态']) {
      expect(screen.getByRole('columnheader', { name: label })).toBeInTheDocument()
    }
    expect(screen.getByText('士兰微')).toBeInTheDocument()
    expect(screen.getByText('已复盘')).toBeInTheDocument()
    // 空的关闭周期部分展示行内空态而非空白。
    expect(screen.getByText('本部分暂无数据')).toBeInTheDocument()
  })
})

describe('Delivery Scorecard Template Dialog', () => {
  it('目录项中文化展示并支持交付记分卡的范围字段', () => {
    const onSave = vi.fn().mockResolvedValue(undefined)
    render(<AnalysisTemplateDialog catalogs={[
      { catalog_id: 'operations_overview', label: '经营概览', allowed_scope_fields: ['days'] },
      { catalog_id: 'delivery_scorecard', label: '交付记分卡', allowed_scope_fields: ['days', 'job_id'] },
    ]} onCancel={noop} onSave={onSave} />)

    fireEvent.change(screen.getByLabelText('分析类型'), { target: { value: 'delivery_scorecard' } })
    expect(screen.getByLabelText('统计周期（天）')).toBeInTheDocument()
    expect(screen.getByLabelText('岗位 ID')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('名称'), { target: { value: '每周交付记分卡' } })
    fireEvent.change(screen.getByLabelText('岗位 ID'), { target: { value: '10' } })
    fireEvent.click(screen.getByRole('button', { name: '创建' }))

    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      name: '每周交付记分卡', catalog_id: 'delivery_scorecard', scope: { job_id: 10 },
    }))
  })
})
