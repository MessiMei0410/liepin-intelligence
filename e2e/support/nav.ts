import type { Locator, Page } from '@playwright/test'
import { expect } from './fixtures'

// 数据锚点（来自正式库只读副本，每次运行由 global-setup 新鲜复制）：
// 岗位 #154 士兰微·技术市场经理/总监（PC电源），人选数随真实业务增长（断言从 API 取期望值）；
// #559 衣**（S1 在推）、#563 唐**（H5 已停止）；
// workflow_1076e0e1d5d5 = blocked + completed_needs_review 的真实业务终态样本。
export const JOB_ID = 154
export const CANDIDATE_ID = 559
export const CANDIDATE_NAME = '衣**'
export const STAGE_BEFORE = 'S1 新增寻访/待复核'
export const STAGE_STOPPED = 'H5 最近寻访/初筛不通过'
export const WORKFLOW_ID = 'workflow_1076e0e1d5d5'

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
  const panel = page.locator('.workflow-panel')
  await expect(panel).toBeVisible()
  await expect(panel.locator('.detail-head h2')).toContainText('士兰微')
  return panel
}
