import { expect, test } from '@playwright/test'

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => window.localStorage.setItem('argus:mock-role', 'operator'))
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto('/submit')
  await expect(page.getByLabel('runner 1 Runner')).toContainText('aisbench')
})

test('在列表末尾添加 Runner：新增后定位并聚焦新行', async ({ page }) => {
  const add = page.getByTestId('add-runner')
  await expect(add).toHaveText('添加另一个 Runner')
  const firstBox = await page.getByTestId('runner-row-1').boundingBox()
  const addBox = await add.boundingBox()
  expect(addBox!.y).toBeGreaterThan(firstBox!.y)

  await add.click()
  await expect(page.getByTestId('runner-row-2')).toBeVisible()
  await expect(page.getByLabel('runner 2 Runner')).toBeFocused()
  await page.getByTestId('remove-runner-2').click()
  await expect(page.getByTestId('runner-row-2')).toHaveCount(0)
  await page.getByRole('button', { name: '撤销' }).click()
  await expect(page.getByTestId('runner-row-2')).toBeVisible()
})

test('示例是直接动作；说明性常驻文案退出页面', async ({ page }) => {
  await expect(page.getByTestId('deploy-fill-example')).toHaveText('填入示例')
  await expect(page.getByTestId('input-1-fill-example')).toHaveText('填入示例')
  await expect(page.locator('[data-testid="deploy-example"]')).toHaveCount(0)
  await expect(page.locator('[data-testid="input-1-example"]')).toHaveCount(0)
  await expect(page.getByTestId('task-name-counter')).toHaveCount(0)

  const main = page.locator('main')
  for (const copy of ['配置服务和测试输入', '平台不解释业务参数', '当前为默认值', '每行一个 JSON 对象', '拖动右下角可调整高度']) {
    await expect(main).not.toContainText(copy)
  }
})
