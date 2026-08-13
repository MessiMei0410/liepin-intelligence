import type { Locator, Page } from '@playwright/test'
import { expect } from './fixtures'

// 数据锚点（来自正式库只读副本，每次运行由 global-setup 新鲜复制）：
// 岗位 #154 士兰微·技术市场经理/总监（PC电源），人选数随真实业务增长（断言从 API 取期望值）；
// #559 衣**（S1 在推）、#563 唐**（H5 已停止）；
// workflow_bcab82502825 = 第 7 轮 blocked + completed_needs_review 样本（未归档、有 strategy_v2/复盘/渠道漏斗行）。
// 注意：业务侧归档旧轮次后需把锚点换成最新未归档 blocked 工作流（第 3 轮 1076e0e1d5d5 于 2026-07-23 09:50 归档）。
export const JOB_ID = 154
export const CANDIDATE_ID = 559
export const CANDIDATE_NAME = '衣**'
export const STAGE_BEFORE = 'S1 新增寻访/待复核'
export const STAGE_STOPPED = 'H5 最近寻访/初筛不通过'
export const WORKFLOW_ID = 'workflow_bcab82502825'
// S5-2 Mapping 任务卡锚点：第 9 轮 blocked + completed_needs_review，策略复盘含 escalate_mapping 步，
// 且产物已有真实任务卡 mapping_task_workflow_15fc23c21ce8（job 154，17 团队 / 10 候选；
// 人选状态随顾问真实使用漂移，用例按「有确认按钮的卡」动态选 pending，不钉人名）。
export const MAPPING_WORKFLOW_ID = 'workflow_15fc23c21ce8'

// 评估 tab 截图锚点（2026-08-13 改版回归）：
// #716 王**（job 142 士兰微·电源专家）：判人评估 artifact（五维 + 顾问口径摘要）+ 深度匹配评估
// （criteria 27 条 = 硬门槛 8 / 核心能力 15 / 软偏好 4，强 6 弱 4），内容最全；
// #510 张FisherMan（job 111，H5 初筛不通过）：无判人评估，仅候选人匹配初评（candidate_intelligence，
// 强 4 弱 1），「匹配初评 + 还没做过评估」空态样本。
// 注意：评估内容随顾问真实使用/批量刷新漂移，锚点若失配按当前库实况换样本。
export const ASSESSMENT_CANDIDATE_ID = 716
export const ASSESSMENT_CANDIDATE_NAME = '王**'
export const ASSESSMENT_EMPTY_CANDIDATE_ID = 510
export const ASSESSMENT_EMPTY_CANDIDATE_NAME = '张FisherMan'

// 真实 UI 路径进评估 tab：人选列表 tab → 搜索（job_candidates.id 参与索引且全库唯一）→
// 点行进人选详情 → 切「评估」tab，返回评估区 section。stopped=true 时先切「已停止」范围。
export async function openCandidateAssessment(
  page: Page,
  candidateId: number,
  candidateName: string,
  { stopped = false }: { stopped?: boolean } = {},
): Promise<Locator> {
  await page.goto('/asa-app')
  await expect(page.locator('header.topbar')).toContainText('ASA Agent 在线')
  await page.locator('aside.nav').getByRole('button', { name: '人选列表' }).click()
  await page.getByRole('searchbox', { name: '搜索候选人' }).fill(String(candidateId))
  if (stopped) await page.getByRole('button', { name: /^已停止/ }).click()
  const row = page.getByRole('row', { name: `打开候选人 ${candidateName}` })
  await expect(row).toBeVisible()
  await row.click()
  const panel = page.locator('.candidate-panel')
  await expect(panel).toBeVisible()
  await expect(panel.locator('.detail-head h2')).toHaveText(candidateName)
  await panel.locator('.candidate-tabs').getByRole('button', { name: '评估' }).click()
  const assessment = panel.locator('section.assessment')
  await expect(assessment).toBeVisible()
  return assessment
}

export async function openJob(page: Page, jobId = JOB_ID): Promise<Locator> {
  await page.goto(`/asa-app#job=${jobId}`)
  const panel = page.locator('.job-detail-panel')
  await expect(panel).toBeVisible()
  await expect(panel.locator('.detail-head h2')).toContainText('技术市场经理')
  return panel
}

export async function openCandidateFromJob(page: Page, candidateName = CANDIDATE_NAME): Promise<Locator> {
  const jobPanel = await openJob(page)
  await jobPanel.locator('.job-candidate-list button', { hasText: candidateName }).click()
  const panel = page.locator('.candidate-panel')
  await expect(panel).toBeVisible()
  await expect(panel.locator('.detail-head h2')).toHaveText(candidateName)
  return panel
}

export async function openWorkflow(page: Page, workflowId = WORKFLOW_ID): Promise<Locator> {
  await page.goto(`/asa-app#workflow=${workflowId}`)
  const panel = page.locator('.compact-workflow-dialog')
  await expect(panel).toBeVisible()
  await expect(panel.locator('.compact-workflow-head h2')).toContainText('士兰微')
  return panel
}

export async function openWorkflowDetail(page: Page, workflowId = WORKFLOW_ID): Promise<Locator> {
  const compact = await openWorkflow(page, workflowId)
  await compact.getByRole('button', { name: '查看' }).click()
  await compact.getByRole('menuitem', { name: '完整详情' }).click()
  const panel = page.locator('.workflow-panel')
  await expect(panel).toBeVisible()
  await expect(panel.locator('.detail-head h2')).toContainText('士兰微')
  return panel
}
