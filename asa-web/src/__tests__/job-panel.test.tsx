import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { JobPanel, JobListSection } from '../panels/JobPanel'
import type { Candidate, JobDetail } from '../api'

vi.mock('../panels/JobProfileInsights', () => ({ JobProfileInsights: () => null }))
vi.mock('../panels/RecommendationMetricsCard', () => ({ RecommendationMetricsCard: () => null }))
vi.mock('../panels/JobWeeklyReport', () => ({ JobWeeklyReport: () => null }))
vi.mock('../panels/SourcingAdjustments', () => ({ SourcingAdjustments: () => null }))

const makeCandidate = (id: number, extra: Partial<Candidate> = {}): Candidate => ({
  id,
  person_id: id + 1000,
  name: `候选人${id}`,
  current_company: '示例科技',
  current_title: '高级工程师',
  clean_stage: 'S1 待复核',
  flow_bucket: '初筛',
  source_type: 'liepin',
  updated_at: '2026-08-05T10:00:00',
  ...extra,
})

const makeJob = (overrides: Partial<JobDetail> = {}): JobDetail => ({
  id: 154,
  title: '技术市场经理',
  client: '士兰微',
  location: '杭州',
  status: '进行中',
  priority: 'P0',
  candidate_count: 0,
  active_candidate_count: 0,
  position: {
    location: '杭州',
    salary: '30-50K',
    headcount: 1,
    deadline: '2026-09-30',
    department: '市场部',
    education: '本科',
    experience: '5 年以上',
    hard_requirements: ['A', 'B'],
    ability_keywords: ['C'],
    target_companies: ['X公司'],
    exclusions: ['Y'],
    pitch_points: ['卖点1'],
  },
  profile: {
    hard_requirements: [],
    ability_keywords: [],
    target_companies: [],
    exclusion_tags: [],
    pitch_points: [],
    risk_points: [],
    education_requirement: '',
    experience_requirement: '',
    jd_analysis_summary: '岗位概述',
  },
  funnel: { total: 0, active: 0, contacted: 0, recommended: 0, stopped: 0 },
  stages: [],
  candidates: [],
  search_experiments: [],
  events: [],
  followups: [],
  latest_effective_strategy: null,
  ...overrides,
})

describe('JobPanel 岗位详情', () => {
  it('渲染漏斗、岗位概况与阶段分布数量', () => {
    const job = makeJob({
      funnel: { total: 12, active: 8, contacted: 5, recommended: 2, stopped: 4 },
      stages: [{ stage: 'S1 待复核', count: 6 }, { stage: 'H5 已停止', count: 3 }],
    })
    render(<JobPanel value={job} close={() => undefined} openCandidate={vi.fn()} />)

    const funnel = screen.getByLabelText('岗位漏斗')
    expect(within(funnel).getByText('12')).toBeInTheDocument()
    expect(within(funnel).getByText('4')).toBeInTheDocument()
    expect(screen.getByText('岗位概述')).toBeInTheDocument()
    expect(screen.getByText('30-50K')).toBeInTheDocument()
    expect(screen.getByText('2 个阶段')).toBeInTheDocument()
    expect(screen.getByText('S1 待复核')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
  })

  it('硬性要求等列表区展示数量徽标，空列表不渲染区块', () => {
    render(<JobListSection title="硬性要求" items={['A', 'B', 'C']} />)
    expect(screen.getByText('3 项')).toBeInTheDocument()
    expect(screen.getByText('A')).toBeInTheDocument()

    const { container } = render(<JobListSection title="目标公司" items={[]} />)
    expect(container.querySelector('.job-detail-section')).toBeNull()
  })

  it('岗位 Brief 只汇总已有岗位事实，并明确提示缺失的启动条件', () => {
    render(<JobPanel value={makeJob({ profile: { hard_requirements: [], target_companies: [], exclusion_tags: [], jd_analysis_summary: '', pitch_points: [], ability_keywords: [], risk_points: [] }, position: {} })} close={() => undefined} openCandidate={vi.fn()} />)
    const brief = screen.getByLabelText('岗位 Brief')
    expect(within(brief).getByText('岗位职责摘要待补充')).toBeInTheDocument()
    expect(within(brief).getByText(/硬性要求尚未确认/)).toBeInTheDocument()
    expect(within(brief).getByText(/尚未形成生效寻访策略/)).toBeInTheDocument()
  })

  it('阶段分布空态给出说明', () => {
    render(<JobPanel value={makeJob()} close={() => undefined} openCandidate={vi.fn()} />)
    expect(screen.getByText('当前岗位还没有候选人阶段记录。')).toBeInTheDocument()
  })

  it('寻访策略分区展示约束/关键词/公司/职级与数量', () => {
    const job = makeJob({
      latest_effective_strategy: {
        status: 'effective',
        plan_version: 2,
        generated_at: '2026-08-01T09:00:00',
        company_tiers: [{ path: 'A', tier: 'T1', companies: ['公司1', '公司2'], rationale: 'r' }],
        level_mapping: { accepted_levels: ['P6', 'P7'] },
        keyword_groups: [{ group: 'g', targets: 't', terms: ['关键词A', '关键词B'] }],
        expectation: { fallback_plan: '扩展搜索' },
        consultant_constraints: [{ type: 'exclude', rule: '不要外包' }],
        audit: { workflow_id: 'wf', artifact_id: 'art', schema_version: '1' },
      },
    })
    render(<JobPanel value={job} close={() => undefined} openCandidate={vi.fn()} />)

    const section = screen.getByLabelText('当前寻访策略')
    expect(within(section).getByText('v2')).toBeInTheDocument()
    expect(within(section).getByText(/生成于 2026-08-01 09:00/)).toBeInTheDocument()
    expect(within(section).getByText('顾问约束')).toBeInTheDocument()
    expect(within(section).getByText('不要外包')).toBeInTheDocument()
    expect(within(section).getByText('关键词A')).toBeInTheDocument()
    expect(within(section).getByText('公司1')).toBeInTheDocument()
    expect(within(section).getByText('P6')).toBeInTheDocument()
    expect(within(section).getByText('扩展路径：扩展搜索')).toBeInTheDocument()
    expect(within(section).getAllByText('2 个')).toHaveLength(2)
    expect(within(section).getByText('2 家')).toBeInTheDocument()
  })

  it('无生效策略时展示空态', () => {
    render(<JobPanel value={makeJob()} close={() => undefined} openCandidate={vi.fn()} />)
    expect(screen.getByText('当前岗位还没有生效的寻访策略。')).toBeInTheDocument()
  })

  it('寻访记录默认截断前 8 条并说明数量，可展开全部再收起', () => {
    const experiments = Array.from({ length: 15 }, (_, index) => ({
      id: index + 1,
      query: `关键词${index}`,
      channel: 'xsaas',
      result_count: 10,
      extracted_count: 5,
      recommended_count: 2,
      updated_at: '2026-08-05T10:00:00',
    }))
    render(<JobPanel value={makeJob({ search_experiments: experiments })} close={() => undefined} openCandidate={vi.fn()} />)

    const section = screen.getByLabelText('寻访记录')
    expect(within(section).getAllByText(/关键词\d+/)).toHaveLength(8)
    expect(within(section).getByText('仅显示前 8 条，共 15 条。')).toBeInTheDocument()
    expect(within(section).getAllByText(/X-SaaS/)).toHaveLength(8)

    fireEvent.click(within(section).getByRole('button', { name: '展开全部 15 条' }))
    expect(within(section).getAllByText(/关键词\d+/)).toHaveLength(15)
    expect(within(section).getByText('已展开全部 15 条。')).toBeInTheDocument()

    fireEvent.click(within(section).getByRole('button', { name: '收起' }))
    expect(within(section).getAllByText(/关键词\d+/)).toHaveLength(8)
  })

  it('寻访记录空态说明', () => {
    render(<JobPanel value={makeJob()} close={() => undefined} openCandidate={vi.fn()} />)
    expect(screen.getByText('暂无历史寻访记录。')).toBeInTheDocument()
  })

  it('岗位人选全量渲染在限高列表内，打开动作可靠传回候选人 id', () => {
    const candidates = Array.from({ length: 30 }, (_, index) => makeCandidate(index + 1))
    const openCandidate = vi.fn()
    render(<JobPanel value={makeJob({ candidates })} close={() => undefined} openCandidate={openCandidate} />)

    const list = screen.getByLabelText('岗位人选列表')
    expect(within(list).getAllByRole('button', { name: /打开候选人/ })).toHaveLength(30)
    expect(screen.getByText('30 人')).toBeInTheDocument()
    fireEvent.click(within(list).getByRole('button', { name: '打开候选人 候选人5' }))
    expect(openCandidate).toHaveBeenCalledTimes(1)
    expect(openCandidate).toHaveBeenCalledWith(5)
    expect(within(list).getAllByText('S1 待复核')).toHaveLength(30)
  })

  it('岗位人选空态给出引导', () => {
    render(<JobPanel value={makeJob()} close={() => undefined} openCandidate={vi.fn()} />)
    expect(screen.getByText(/当前岗位还没有人选，可交给 Agent 启动寻访/)).toBeInTheDocument()
  })

  it('待办默认截断前 8 条并说明数量，可展开全部', () => {
    const followups = Array.from({ length: 13 }, (_, index) => ({
      id: index + 1,
      candidate_name: `候选人${index}`,
      task_type: '跟进',
      reason: `原因${index}`,
      due_at: '2026-08-10T10:00:00',
    }))
    render(<JobPanel value={makeJob({ followups })} close={() => undefined} openCandidate={vi.fn()} />)

    expect(screen.getAllByText(/原因\d+/)).toHaveLength(8)
    expect(screen.getByText('仅显示前 8 条，共 13 条。')).toBeInTheDocument()
    expect(screen.getAllByText('截止 2026-08-10 10:00')).toHaveLength(8)

    fireEvent.click(screen.getByRole('button', { name: '展开全部 13 条' }))
    expect(screen.getAllByText(/原因\d+/)).toHaveLength(13)
    expect(screen.queryByText('仅显示前 8 条，共 13 条。')).not.toBeInTheDocument()
  })

  it('待办空态说明', () => {
    render(<JobPanel value={makeJob()} close={() => undefined} openCandidate={vi.fn()} />)
    expect(screen.getByText('当前没有岗位待办')).toBeInTheDocument()
  })

  it('最近动态默认截断前 10 条，状态文案本地化，可展开全部', () => {
    const events = Array.from({ length: 25 }, (_, index) => ({
      id: index + 1,
      event_type: 'candidate_update',
      event_status: 'pending_review',
      event_time: '2026-08-05T10:00:00',
      summary: `事件${index}`,
    }))
    render(<JobPanel value={makeJob({ events })} close={() => undefined} openCandidate={vi.fn()} />)

    expect(screen.getAllByText(/事件\d+/)).toHaveLength(10)
    expect(screen.getAllByText('待复核')).toHaveLength(10)
    expect(screen.getByText('仅显示前 10 条，共 25 条。')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '展开全部 25 条' }))
    expect(screen.getAllByText(/事件\d+/)).toHaveLength(25)
  })

  it('动态空态说明', () => {
    render(<JobPanel value={makeJob()} close={() => undefined} openCandidate={vi.fn()} />)
    expect(screen.getByText('当前没有业务动态')).toBeInTheDocument()
  })
})
