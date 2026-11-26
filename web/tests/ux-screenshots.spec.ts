import { expect, test, type Page } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

/**
 * UX 改造的桌面宽度验收（任务要求：1280 / 1440 / 1920）：
 *  - 概览 / 提交 / 成功详情 / 失败详情 四个关键页面各截一张全页图（.data/ux-overhaul/）；
 *  - 无横向溢出（documentElement.scrollWidth ≤ clientWidth）；
 *  - 无浏览器脚本错误（console.error / pageerror 计数为 0）。
 */

const OUT_DIR = join(process.cwd(), '.data', 'ux-overhaul')

const WIDTHS = [1280, 1440, 1920] as const

/** 四个关键页面：名称 → 路径与就绪标志 */
const PAGES: Array<{ key: string; path: string; ready: (page: Page) => Promise<void> }> = [
  {
    key: 'overview',
    path: '/',
    ready: async (page) => {
      await expect(page.getByTestId('pagination-total')).toBeVisible({ timeout: 20_000 })
    },
  },
  {
    key: 'submit',
    path: '/submit',
    ready: async (page) => {
      await expect(page.getByTestId('submit-footer')).toBeVisible({ timeout: 20_000 })
      await expect(page.getByLabel('runner 1 Runner')).toContainText('aisbench', { timeout: 20_000 })
    },
  },
  {
    key: 'detail-success',
    path: '/tasks/t-20261105-0006',
    ready: async (page) => {
      await expect(page.getByTestId('result-primary-action')).toBeVisible({ timeout: 20_000 })
    },
  },
  {
    key: 'detail-failed',
    path: '/tasks/t-20261105-0003',
    ready: async (page) => {
      await expect(page.getByTestId('result-primary-action')).toBeVisible({ timeout: 20_000 })
    },
  },
]

for (const width of WIDTHS) {
  for (const target of PAGES) {
    test(`${width}px · ${target.key}：无横向溢出、无脚本错误、截图落盘`, async ({ page }) => {
      mkdirSync(OUT_DIR, { recursive: true })
      const consoleErrors: string[] = []
      page.on('console', (message) => {
        if (message.type() === 'error') consoleErrors.push(message.text())
      })
      page.on('pageerror', (error) => consoleErrors.push(String(error)))

      await page.setViewportSize({ width, height: 1000 })
      await page.addInitScript(() => window.localStorage.setItem('argus:mock-role', 'operator'))
      await page.goto(target.path)
      await target.ready(page)
      await page.waitForTimeout(500)

      // 横向溢出检查（#117 验收：提高字号不引入溢出/裁切）
      const overflow = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }))
      expect(overflow.scrollWidth, `${width}px ${target.key} 不应有横向溢出`).toBeLessThanOrEqual(overflow.clientWidth)

      expect(consoleErrors, `${width}px ${target.key} 不应有脚本错误：${consoleErrors.join(' | ')}`).toHaveLength(0)

      await page.screenshot({ path: join(OUT_DIR, `${target.key}-${width}.png`), fullPage: true })
    })
  }
}
