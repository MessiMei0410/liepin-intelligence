import { expect, skipIfNoBackend, test } from '../support/fixtures'
import {
  ASSESSMENT_CANDIDATE_ID,
  ASSESSMENT_CANDIDATE_NAME,
  openCandidateAssessment,
} from '../support/nav'

test.beforeEach(() => skipIfNoBackend())

test('顾问采纳评估后立即回读候选人业务时间线', async ({ page }) => {
  const assessment = await openCandidateAssessment(
    page,
    ASSESSMENT_CANDIDATE_ID,
    ASSESSMENT_CANDIDATE_NAME,
  )

  await assessment.getByRole('button', { name: '采纳' }).click()
  await expect(assessment.getByRole('status')).toContainText('已采纳已记录')

  const panel = page.locator('.candidate-panel')
  await panel.locator('.candidate-tabs').getByRole('button', { name: '记录' }).click()
  await expect(panel.locator('.timeline-main')).toContainText('判人评估顾问动作：已采纳')
})
