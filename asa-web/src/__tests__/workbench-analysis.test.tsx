import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, type AnalysisResult, type AnalysisTemplate, type AnalysisTemplateRun, type AnalysisTrend, type Workbench } from '../api'
import { AnalysisTemplateDialog } from '../components/AnalysisTemplateDialog'
import { AnalysisWorkspace } from '../pages/AnalysisWorkspace'
import { TodayWorkbench } from '../pages/TodayWorkbench'

const workbench: Workbench = {
  ok: true, version: 'v1', summary: { pending: 1, running: 0, delivered: 1, total: 2, decision: 1, waiting_client: 0, risk: 0 },
  items: [
    {
      item_key: 'approval:a1', source_revision: 'r1', kind: 'approval', lane: 'decision', priority_score: 20000,
      title: '士兰微电源专家寻访', subtitle: '批准多渠道寻访', status_label: 'R3 待审批', reason: '外部动作需由顾问确认',
      source_label: '审批', updated_at: '2026-08-03 09:00:00', inbox_state: 'unread',
      primary_action: { type: 'open_workflow', id: 'workflow-1', label: '查看并审批' },
    },
    {
      item_key: 'analysis:a1', source_revision: 'r2', kind: 'analysis', lane: 'delivered', priority_score: 0,
      title: '4 项人选推进需要关注', subtitle: '经营概览', status_label: '已交付', reason: '',
      source_label: '分析', updated_at: '2026-08-03 09:05:00', inbox_state: 'unread',
      primary_action: { type: 'open_analysis', id: 'analysis-1', label: '查看分析' },
    },
  ],
}

const analysis: AnalysisResult = {
  schema_version: 'analysis_result_v1', run_id: 'analysis-1', catalog_id: 'job_health', catalog_version: '2026-08-03',
  status: 'completed', question: '这个岗位健康吗？', scope: { job_id: 154 }, data_as_of: '2026-08-03T09:00:00+08:00',
  headline: '岗位共有 8 名有效人选',
  metrics: [
    { id: 'active', label: '有效人选', value: 8, unit: 'count', definition_id: 'asa.active', definition_version: '2026-08-03' },
    { id: 'rate', label: '推荐率', value: null, unit: 'ratio', definition_id: 'asa.rate', definition_version: '2026-08-03' },
  ],
  sections: [{ type: 'table', title: '岗位漏斗', columns: ['title', 'active'], rows: [{ title: '电源专家', active: 8 }] }],
  references: [], caveats: ['分母为零时返回 null。'], truncated: false, suggested_actions: [], supersedes_run_id: null,
}

const trend: AnalysisTrend = {
  ok: true, template_id: 'template-1', name: '每日岗位健康', catalog_id: 'job_health', run_count: 2,
  runs: [],
  series: [{
    metric_id: 'active', label: '有效人选', unit: 'count', latest: 8, previous: 6, delta: 2, delta_ratio: 0.3333,
    points: [{ run_id: 'analysis-0', at: '2026-08-02', value: 6 }, { run_id: 'analysis-1', at: '2026-08-03', value: 8 }],
  }],
}

const template: AnalysisTemplate = {
  template_id: 'template-1', name: '每日岗位健康', catalog_id: 'job_health', question: '',
  scope: {}, enabled: true, schedule_kind: 'manual', schedule_enabled: false,
  schedule_time: '09:00', schedule_weekday: 0, timezone: 'Asia/Shanghai',
}

const runItem: AnalysisTemplateRun = {
  template_run_id: 'run-1', template_id: 'template-1', trigger: 'manual', status: 'completed',
  started_at: '2026-08-05T09:00:00+08:00', headline: '上周岗位健康', error: null,
}

describe('Today Workbench', () => {
  it('按 lane 展示唯一主动作，并可运行固定分析', () => {
    const onAction = vi.fn()
    const onRunTemplate = vi.fn()
    render(<TodayWorkbench dashboard={{ counts: { active_jobs: 3, candidates: 12 } }} workbench={workbench}
      templates={[{ template_id: 'template-1', name: '每日经营概览', catalog_id: 'operations_overview', question: '', scope: {}, enabled: true, schedule_kind: 'daily', schedule_enabled: true, schedule_time: '09:00', schedule_weekday: 0, timezone: 'Asia/Shanghai' }]}
      onAction={onAction} onQuickAnalysis={() => {}} onRunTemplate={onRunTemplate} onOpenTemplate={() => {}} onCreateTemplate={() => {}} onManageTemplate={() => {}} />)

    expect(screen.getByText('R3 待审批')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /查看并审批/ }))
    expect(onAction).toHaveBeenCalledWith(workbench.items[0])

    fireEvent.click(screen.getByRole('button', { name: /固定分析/ }))
    expect(screen.getByText('每日经营概览')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '运行' }))
    expect(onRunTemplate).toHaveBeenCalledWith('template-1')
  })

  it('按五个分组组织工作台，空分组展示真实空态', () => {
    const fiveLaneWorkbench: Workbench = {
      ...workbench,
      summary: { pending: 1, running: 1, delivered: 1, total: 4, decision: 1, waiting_client: 1, risk: 0 },
      items: [
        workbench.items[0],
        workbench.items[1],
        {
          ...workbench.items[0], item_key: 'candidate:9', kind: 'candidate_action', lane: 'waiting_client', priority_score: 100,
          title: '王先生', subtitle: '士兰微 / 电源专家', status_label: '进行中', reason: '已推荐，待客户反馈',
          source_label: '人选推进', primary_action: { type: 'open_candidate', id: '9', label: '查看' },
        },
        {
          ...workbench.items[0], item_key: 'workflow:w1', kind: 'workflow', lane: 'running', priority_score: 6000,
          title: '第 3 轮寻访', subtitle: '执行多渠道寻访', status_label: '运行中', reason: '',
          source_label: 'Agent 任务', primary_action: { type: 'open_workflow', id: 'w1', label: '查看进度' },
        },
      ],
    }
    render(<TodayWorkbench dashboard={{ counts: {} }} workbench={fiveLaneWorkbench}
      templates={[]} onAction={() => {}} onQuickAnalysis={() => {}} onRunTemplate={() => {}} onOpenTemplate={() => {}} onCreateTemplate={() => {}} onManageTemplate={() => {}} />)

    // 五个业务分组都在分段导航里，默认落在待判断。
    for (const label of ['待判断', '运行中', '待客户', '风险/逾期', '最近交付']) {
      expect(screen.getByRole('button', { name: new RegExp(label.replace('/', '\\/')) })).toBeInTheDocument()
    }
    expect(screen.getByText('R3 待审批')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /待客户/ }))
    expect(screen.getByText('王先生')).toBeInTheDocument()
    expect(screen.getByText('已推荐，待客户反馈')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /风险\/逾期/ }))
    expect(screen.getByText('当前没有风险或逾期事项')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /最近交付/ }))
    expect(screen.getByText('4 项人选推进需要关注')).toBeInTheDocument()
  })

  it('每页最多展示 10 条并支持翻页', () => {
    const pendingItems = Array.from({ length: 12 }, (_, index) => ({
      ...workbench.items[0],
      item_key: `candidate:${index + 1}`,
      title: `待处理人选 ${index + 1}`,
    }))
    render(<TodayWorkbench dashboard={{ counts: {} }}
      workbench={{ ...workbench, summary: { pending: 12, running: 0, delivered: 0, total: 12 }, items: pendingItems }}
      templates={[]} onAction={() => {}} onQuickAnalysis={() => {}} onRunTemplate={() => {}} onOpenTemplate={() => {}} onCreateTemplate={() => {}} onManageTemplate={() => {}} />)

    expect(screen.getAllByRole('article')).toHaveLength(10)
    expect(screen.getByText('第 1 / 2 页 · 共 12 项')).toBeInTheDocument()
    expect(screen.queryByText('待处理人选 11')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(screen.getAllByRole('article')).toHaveLength(2)
    expect(screen.getByText('待处理人选 11')).toBeInTheDocument()
  })

  it('数据被截断时区分已加载数量与真实总数', () => {
    render(<TodayWorkbench dashboard={{ counts: {} }}
      workbench={{ ...workbench, summary: { pending: 445, running: 0, delivered: 3, total: 448 }, returned_count: 300, truncated: true }}
      templates={[]} onAction={() => {}} onQuickAnalysis={() => {}} onRunTemplate={() => {}} onOpenTemplate={() => {}} onCreateTemplate={() => {}} onManageTemplate={() => {}} />)

    expect(screen.getByText(/已加载 300 \/ 448/)).toBeInTheDocument()
  })
})

describe('Analysis Workspace', () => {
  it('保留零分母语义并暴露刷新、导出和返回命令', () => {
    const close = vi.fn(), refresh = vi.fn(), exportReport = vi.fn()
    render(<AnalysisWorkspace result={analysis} close={close} refresh={refresh} exportReport={exportReport} />)
    expect(screen.getByText('数据不足')).toBeInTheDocument()
    expect(screen.getByText('电源专家')).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: '职位' })).toHaveAttribute('scope', 'col')
    expect(screen.getByRole('columnheader', { name: '有效人选' })).toHaveAttribute('scope', 'col')
    fireEvent.click(screen.getByRole('button', { name: '刷新' }))
    fireEvent.click(screen.getByRole('button', { name: '导出' }))
    fireEvent.click(screen.getByRole('button', { name: '返回' }))
    expect(refresh).toHaveBeenCalledOnce()
    expect(exportReport).toHaveBeenCalledOnce()
    expect(close).toHaveBeenCalledOnce()
  })

  it('展示固定分析的历史变化', () => {
    render(<AnalysisWorkspace result={analysis} trend={trend} close={() => {}} refresh={() => {}} exportReport={() => {}} />)
    expect(screen.getByRole('region', { name: '变化趋势' })).toBeInTheDocument()
    expect(screen.getByText('2 次固定分析')).toBeInTheDocument()
    expect(screen.getByText('+2')).toBeInTheDocument()
  })

  it('分析表格保留 Mapping 直挖渠道口径', () => {
    render(<AnalysisWorkspace result={{
      ...analysis,
      sections: [{ type: 'table', title: '渠道质量', columns: ['channel', 'intaked'], rows: [{ channel: 'mapping', intaked: 1 }] }],
    }} close={() => {}} refresh={() => {}} exportReport={() => {}} />)

    expect(screen.getByRole('columnheader', { name: '渠道' })).toBeInTheDocument()
    expect(screen.getByText('Mapping 直挖')).toBeInTheDocument()
    expect(screen.queryByText('人才库')).not.toBeInTheDocument()
  })

  it('失败或过期结果提供明确重试且禁止导出', () => {
    const refresh = vi.fn()
    render(<AnalysisWorkspace result={{ ...analysis, status: 'expired', metrics: [], sections: [] }} close={() => {}} refresh={refresh} exportReport={() => {}} />)

    expect(screen.getByText('分析结果已过期')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '导出' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '重试分析' }))
    expect(refresh).toHaveBeenCalledOnce()
  })

  it('失败结果展示原因、禁止导出并允许重试', () => {
    render(<AnalysisWorkspace result={{ ...analysis, status: 'failed', metrics: [], sections: [], caveats: ['数据源超时'] }} close={() => {}} refresh={() => {}} exportReport={() => {}} />)

    expect(screen.getByText('分析未完成')).toBeInTheDocument()
    expect(screen.getByText('数据源超时')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '导出' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '重试分析' })).toBeEnabled()
  })

  it('部分完成结果保留查看与导出，并提示数据缺口', () => {
    render(<AnalysisWorkspace result={{ ...analysis, status: 'partial', caveats: ['部分指标暂缺'] }} close={() => {}} refresh={() => {}} exportReport={() => {}} />)

    expect(screen.getByText('分析部分完成')).toBeInTheDocument()
    expect(screen.getByText('部分指标暂缺')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '导出' })).toBeEnabled()
  })

  it('无指标无表格时展示真实空态而非空白', () => {
    render(<AnalysisWorkspace result={{ ...analysis, metrics: [], sections: [] }} close={() => {}} refresh={() => {}} exportReport={() => {}} />)

    expect(screen.getByText('当前范围没有可展示数据')).toBeInTheDocument()
  })

  it('空表格部分渲染行内空态', () => {
    render(<AnalysisWorkspace result={{ ...analysis, sections: [{ type: 'table', title: '岗位漏斗', columns: ['title'], rows: [] }] }} close={() => {}} refresh={() => {}} exportReport={() => {}} />)

    expect(screen.getByText('本部分暂无数据')).toBeInTheDocument()
  })

  it('截断结果给出明确提示', () => {
    render(<AnalysisWorkspace result={{ ...analysis, truncated: true }} close={() => {}} refresh={() => {}} exportReport={() => {}} />)

    expect(screen.getByText('结果已截断')).toBeInTheDocument()
  })

  it('忙碌期间两个操作按钮互斥禁用并标记 aria-busy', () => {
    const { container } = render(<AnalysisWorkspace result={analysis} busy="export" close={() => {}} refresh={() => {}} exportReport={() => {}} />)

    expect(container.querySelector('.analysis-workspace')).toHaveAttribute('aria-busy', 'true')
    expect(screen.getByRole('button', { name: '刷新' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '导出' })).toBeDisabled()
  })
})

describe('Analysis Template Dialog', () => {
  afterEach(() => vi.restoreAllMocks())

  it('通过结构化控件创建每日固定分析', () => {
    const onSave = vi.fn().mockResolvedValue(undefined)
    render(<AnalysisTemplateDialog catalogs={[{
      catalog_id: 'operations_overview', label: '经营概览', allowed_scope_fields: ['days'],
    }]} onCancel={() => {}} onSave={onSave} />)

    fireEvent.change(screen.getByLabelText('名称'), { target: { value: '每日经营变化' } })
    fireEvent.change(screen.getByLabelText('统计周期（天）'), { target: { value: '14' } })
    fireEvent.click(screen.getByRole('button', { name: '每天' }))
    fireEvent.click(screen.getByLabelText('启用自动执行'))
    fireEvent.click(screen.getByRole('button', { name: '创建' }))

    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      name: '每日经营变化', catalog_id: 'operations_overview', scope: { days: 14 },
      schedule_kind: 'daily', schedule_enabled: true, timezone: 'Asia/Shanghai',
    }))
  })

  it('执行频率分段按钮带 pressed 语义', () => {
    render(<AnalysisTemplateDialog catalogs={[]} onCancel={() => {}} onSave={vi.fn().mockResolvedValue(undefined)} />)

    expect(screen.getByRole('button', { name: '手动' })).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(screen.getByRole('button', { name: '每天' }))
    expect(screen.getByRole('button', { name: '每天' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: '手动' })).toHaveAttribute('aria-pressed', 'false')
  })

  it('运行记录请求期间显示加载中，完成后收起', async () => {
    let resolveRuns: (value: { ok: boolean; items: AnalysisTemplateRun[] }) => void = () => undefined
    vi.spyOn(api, 'analyticsTemplateRuns').mockReturnValue(new Promise(resolve => { resolveRuns = resolve }))
    render(<AnalysisTemplateDialog catalogs={[]} template={template} onCancel={() => {}} onSave={vi.fn().mockResolvedValue(undefined)} />)

    expect(screen.getByRole('status')).toHaveTextContent('正在读取运行记录')
    resolveRuns({ ok: true, items: [runItem] })

    expect(await screen.findByText('上周岗位健康')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.getByText('最近 1 次')).toBeInTheDocument()
  })

  it('运行记录加载失败可原地重试', async () => {
    const runsRequest = vi.spyOn(api, 'analyticsTemplateRuns')
      .mockRejectedValueOnce(new Error('服务暂不可用'))
      .mockResolvedValueOnce({ ok: true, items: [runItem] })
    render(<AnalysisTemplateDialog catalogs={[]} template={template} onCancel={() => {}} onSave={vi.fn().mockResolvedValue(undefined)} />)

    expect(await screen.findByText('运行记录加载失败，可稍后重试。')).toBeInTheDocument()
    expect(screen.queryByText('尚无运行记录')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重新加载运行记录' }))

    expect(await screen.findByText('上周岗位健康')).toBeInTheDocument()
    expect(runsRequest).toHaveBeenCalledTimes(2)
  })

  it('删除固定分析需要二次确认', () => {
    const onDelete = vi.fn().mockResolvedValue(undefined)
    render(<AnalysisTemplateDialog catalogs={[]} template={template} onCancel={() => {}} onSave={vi.fn().mockResolvedValue(undefined)} onDelete={onDelete} />)

    fireEvent.click(screen.getByRole('button', { name: '删除' }))
    expect(onDelete).not.toHaveBeenCalled()
    expect(screen.getByText(/删除后不会影响已生成的历史分析结果/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }))
    expect(onDelete).toHaveBeenCalledOnce()
  })
})
