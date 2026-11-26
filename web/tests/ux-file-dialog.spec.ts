import { expect, test } from '@playwright/test'

/** 文件查看始终留在 Task 上下文：URL 可分享，关闭回到同一 Task。 */
test.describe.configure({ mode: 'serial' })

test.beforeEach(async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.addInitScript(() => window.localStorage.setItem('argus:mock-role', 'operator'))
})

test('直接打开文件 URL：Task 详情背景 + 大弹窗，Esc 返回 Task', async ({ page }) => {
  const path = 'attempts/1/runners/1/logs/genet-qwen3-32b-router-0.log'
  await page.goto(`/tasks/t-20261105-0006/file?path=${encodeURIComponent(path)}&attempt=1&seq=1`)

  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText(path)
  await expect(dialog).toContainText('attempt #1')
  await expect(dialog).toContainText('seq 1')
  await expect(page.getByTestId('task-result-verdict')).toBeAttached()
  const box = await dialog.boundingBox()
  expect(box?.width).toBeGreaterThan(1000)

  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await expect(page).toHaveURL(/\/tasks\/t-20261105-0006$/)
})

test('失败日志主动作打开弹窗；关闭按钮回到原 Task', async ({ page }) => {
  await page.goto('/tasks/t-20261105-0003')
  await page.getByTestId('result-primary-action').click()

  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await expect(page).toHaveURL(/\/file\?path=attempts%2F1%2Frunners%2F3%2Flogs%2Fstdout\.log/)
  await expect(dialog).toContainText('stdout.log')

  await page.getByRole('button', { name: '关闭', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  await expect(page).toHaveURL(/\/tasks\/t-20261105-0003$/)
  await expect(page.getByTestId('result-primary-action')).toBeVisible()
})

test('缺少 path：在 Task 上显示可恢复错误，不进入孤立页面', async ({ page }) => {
  await page.goto('/tasks/t-20261105-0006/file')
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('缺少 path')
  await dialog.getByRole('button', { name: '返回 Task 详情' }).click()
  await expect(page).toHaveURL(/\/tasks\/t-20261105-0006$/)
})
