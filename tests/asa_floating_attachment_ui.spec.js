const { test, expect } = require('@playwright/test');

test('floating composer accepts a local attachment without layout overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 700 });
  await page.goto('http://127.0.0.1:8765/asa-floating');
  await page.locator('#attachmentInput').setInputFiles({
    name: '候选人说明.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('候选人具备8年机械设计经验。', 'utf8'),
  });

  const chip = page.locator('.attachment-chip');
  await expect(chip).toContainText('候选人说明.txt');
  await expect(chip).toContainText('已读取附件正文');
  await expect(page.locator('#sendButton')).toBeEnabled();
  await expect(page.locator('#attachButton')).toBeEnabled();

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  expect(overflow).toBe(false);
  await page.screenshot({ path: '/tmp/asa-floating-attachment.png', fullPage: true });
});
