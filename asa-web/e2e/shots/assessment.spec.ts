import { expect, skipIfNoBackend, test } from '../support/fixtures'
import {
  ASSESSMENT_CANDIDATE_ID,
  ASSESSMENT_CANDIDATE_NAME,
  ASSESSMENT_EMPTY_CANDIDATE_ID,
  ASSESSMENT_EMPTY_CANDIDATE_NAME,
  openCandidateAssessment,
} from '../support/nav'

skipIfNoBackend()

async function expectStableViewport(page: import('@playwright/test').Page) {
  const layout = await page.evaluate(() => ({
    viewport: window.innerWidth,
    pageWidth: document.documentElement.scrollWidth,
    bodyWidth: document.body.scrollWidth,
    visibleText: document.body.innerText.trim().length,
  }))
  expect(layout.pageWidth).toBeLessThanOrEqual(layout.viewport)
  expect(layout.bodyWidth).toBeLessThanOrEqual(layout.viewport)
  expect(layout.visibleText).toBeGreaterThan(100)
}

// 2026-08-13 评估 tab 改版（匹配点分析速读区 / 明细全折叠 / 顾问动作上移）截图回归，补 views.spec.ts 盲区。
// 数据全来自 DB 只读副本，无需 mock；等待条件保证异步数据（评估 payload + 校准区）落盘后再拍，避免基线抖动。
// 一律整页截图（与 views.spec.ts 同约定）：面板正文是嵌套滚动容器，元素级截图拼不全、会留空白；
// 长内容通过 scrollIntoViewIfNeeded 分屏拍两张。

test('评估 tab · 深度评估完整态', async ({ page }) => {
  const assessment = await openCandidateAssessment(page, ASSESSMENT_CANDIDATE_ID, ASSESSMENT_CANDIDATE_NAME)
  // 匹配点分析：深度评估速读摘要（评分/等级/建议/标准满足度）+ 强弱匹配点 + 标准分组默认收起
  const fit = assessment.locator('section[aria-label="匹配点分析"]')
  await expect(fit).toBeVisible()
  await expect(fit.locator('.assessment-dim-head')).toContainText('深度评估')
  await expect(fit.locator('.assessment-fit-summary')).toContainText('标准满足度')
  await expect(fit).toContainText('强匹配点')
  await expect(fit).toContainText('弱匹配点')
  await expect(assessment.locator('details.assessment-fit-group').first()).toBeVisible()
  // 顾问动作栏：改版后上移到匹配点分析之后、五维之前
  await expect(assessment.locator('.assessment-actions')).toContainText('顾问动作：')
  await expectStableViewport(page)
  await expect(page).toHaveScreenshot('assessment-full.png')

  // 五维判语摘要态（标题 + 置信度 tag + verdict，明细默认收起）+ 顾问口径摘要 + 证据整包收起 + 校准区
  for (const label of ['职业轨迹', '跳槽质量史', '在同龄人里的位置', '动机与时机', '需要核实的问题']) {
    await expect(assessment.locator(`section[aria-label="${label}"]`)).toBeVisible()
  }
  await expect(assessment.locator('section[aria-label="顾问口径摘要"]')).toBeVisible()
  await expect(assessment.locator('details.assessment-evidence-block')).toBeVisible()
  const calibration = assessment.locator('section[aria-label="评估校准"]')
  // 校准区异步拉取，等其落盘再滚到底拍第二屏
  await expect(calibration).not.toHaveAttribute('aria-busy', 'true')
  await calibration.scrollIntoViewIfNeeded()
  await expectStableViewport(page)
  await expect(page).toHaveScreenshot('assessment-dimensions.png')
})

test('评估 tab · 匹配初评空态', async ({ page }) => {
  const assessment = await openCandidateAssessment(
    page,
    ASSESSMENT_EMPTY_CANDIDATE_ID,
    ASSESSMENT_EMPTY_CANDIDATE_NAME,
    { stopped: true },
  )
  // 仅候选人匹配初评：速读区为「匹配初评」口径，无逐条标准分组
  const fit = assessment.locator('section[aria-label="匹配点分析"]')
  await expect(fit).toBeVisible()
  await expect(fit.locator('.assessment-dim-head')).toContainText('匹配初评')
  await expect(fit).toContainText('强匹配点')
  await expect(fit).toContainText('匹配点来自候选人匹配初评')
  // 还没做过判人评估的空态：说明文案 + 做评估按钮
  const empty = assessment.locator('.assessment-empty')
  await expect(empty).toBeVisible()
  await expect(empty).toContainText('还没做过评估')
  await expect(empty.getByRole('button', { name: '做评估' })).toBeVisible()
  await expectStableViewport(page)
  await expect(page).toHaveScreenshot('assessment-initial-empty.png')
})

test('评估 tab · 硬门槛分组展开', async ({ page }) => {
  const assessment = await openCandidateAssessment(page, ASSESSMENT_CANDIDATE_ID, ASSESSMENT_CANDIDATE_NAME)
  await expect(assessment.locator('section[aria-label="匹配点分析"]')).toBeVisible()
  // 标准分组为原生 <details> 默认收起；展开「硬门槛」验证逐条标准渲染
  const group = assessment.locator('details.assessment-fit-group', { hasText: '硬门槛' })
  await expect(group).toBeVisible()
  await group.locator('summary').click()
  await expect(group).toHaveJSProperty('open', true)
  await expect(group.locator('ul li').first()).toBeVisible()
  await group.scrollIntoViewIfNeeded()
  await expectStableViewport(page)
  await expect(page).toHaveScreenshot('assessment-hard-requirements.png')
})
