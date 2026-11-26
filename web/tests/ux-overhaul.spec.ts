import { expect, test, type Page } from '@playwright/test'

/**
 * UX 改造回归（#111 / #113 / #114 / #115 / #116 / #117）：
 *
 *  1. 列表「进度 / 结果」列：成功 / 部分失败（含跳过）/ 取消 / 运行中 / 部署排队 / 服务不可用
 *     各自的口径与文案（#114；已结束 = succeeded+failed+skipped+canceled，与详情页一致）；
 *  2. 列表与详情计数互相对得上（#114 验收第三条）；
 *  3. 提交页两种服务来源切换 + 草稿保留（#111）；
 *  4. 提交检查区三枚 chip（#111）；
 *  5. 成功 Task 的报告主动作（#115，入口按真实产物能力给）；
 *  6. 失败 Task 的日志主动作 + 人类可读结论（#116，原始码只留诊断条）；
 *  7. precheck 失败（service_unavailable）路径的文案准确（#116）。
 *
 * 全部跑 mock（`VITE_USE_MOCK=1` 默认），不连真后端。
 */

test.describe.configure({ mode: 'serial' })

/** mock 种子数据 → 进度单元格的期望文案（口径见 lib/status.ts progressHeadline / resultSegments）。 */
const EXPECTED_PROGRESS: Record<string, { headline: string; segments: string[] }> = {
  // 全部成功（6 runner 全 succeeded）
  't-20261105-0006': { headline: '已结束 6 / 6', segments: ['成功 6'] },
  // 部分失败：1 失败 + 1 跳过（前序失败），4 成功
  't-20261105-0003': { headline: '已结束 6 / 6', segments: ['失败 1', '成功 4', '跳过 1'] },
  // 取消：1 成功 + 1 被终止 + 1 跳过（canceled）
  't-20261105-0001': { headline: '已结束 3 / 3', segments: ['成功 1', '跳过 1', '取消 1'] },
  // 运行中（外接）：1 成功 + 1 执行中 + 1 排队
  't-20261105-0004': { headline: '已结束 1 / 3 · 执行中', segments: ['成功 1', '执行中 1', '待执行 1'] },
  // 部署排队：deploy runner 在跑（Genet 排队水位），没有已结束 runner —— 不是 "0 / 4"
  't-20261105-0005': { headline: /部署排队中 · 已等待/, segments: ['执行中 1', '待执行 3'] },
  // 服务不可用：precheck 失败，2 个测试 runner 跳过
  't-20261105-0002': { headline: '已结束 5 / 5', segments: ['失败 1', '成功 2', '跳过 2'] },
}

async function openOverview(page: Page, role = 'operator'): Promise<void> {
  await page.addInitScript((r) => window.localStorage.setItem('argus:mock-role', r), role)
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto('/')
  await expect(page.getByTestId('pagination-total')).toBeVisible({ timeout: 20_000 })
}

/* ============================================================ 1/2. 列表进度 / 结果（#114） */

test('① 列表「进度 / 结果」列：六种状态的口径与文案（#114）', async ({ page }) => {
  await openOverview(page)

  for (const [taskId, expected] of Object.entries(EXPECTED_PROGRESS)) {
    const cell = page.getByTestId(`task-progress-${taskId}`)
    await expect(cell, `${taskId} 的进度单元格应可见`).toBeVisible()
    if (expected.headline instanceof RegExp) {
      await expect(cell).toContainText(expected.headline)
    } else {
      await expect(cell).toContainText(expected.headline)
    }
    for (const segment of expected.segments) {
      await expect(cell).toContainText(segment)
    }
  }

  // 不再出现旧的 `succeeded / total` 冒充进度（如 "1 / 6"）：
  await expect(page.getByTestId('task-progress-t-20261105-0003')).not.toContainText('1 / 6')
})

test('② 列表与详情计数互相对应（#114）', async ({ page }) => {
  await openOverview(page, 'admin')
  await page.locator('tbody a[href="/tasks/t-20261105-0003"]').first().click()

  await expect(page.getByTestId('task-result-panel')).toBeVisible({ timeout: 20_000 })
  // 结论卡计数：已结束 / 成功 / 失败 / 跳过 与列表同一口径（dt/dd 拼接无空格，按 token 断言）
  const verdict = page.getByTestId('task-result-verdict')
  await expect(verdict).toContainText('已结束')
  await expect(verdict).toContainText('6 / 6')
  await expect(verdict).toContainText('RUNNER_NONZERO_EXIT')
  // 执行细节页签的概要 KV 同口径
  await page.getByRole('tab', { name: '执行细节' }).click()
  const progress = page.getByTestId('detail-progress')
  await expect(progress).toBeVisible()
  await expect(progress).toContainText('已结束 6 / 6')
  await expect(progress).toContainText('成功 4')
})

/* ============================================================ 3/4. 提交页服务来源与提交检查（#111） */

async function openSubmit(page: Page): Promise<void> {
  await page.addInitScript(() => window.localStorage.setItem('argus:mock-role', 'operator'))
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto('/submit')
  await expect(page.getByLabel('runner 1 Runner')).toContainText('aisbench', { timeout: 15_000 })
}

/** 把 YAML 编辑器内容整体换掉（CodeMirror 是 contenteditable，不能直接 fill） */
async function replaceYaml(page: Page, text: string): Promise<void> {
  const editor = page.getByLabel('部署描述 YAML')
  await editor.click()
  await page.keyboard.press('ControlOrMeta+A')
  await page.keyboard.insertText(text)
}

test('③ 服务来源两形态切换：平台部署 / 已有服务草稿各自保留（#111）', async ({ page }) => {
  await openSubmit(page)

  const mode = page.getByTestId('source-mode')
  // 默认平台部署（genet-deploy），服务来源信息可读：版本 / clean 注入
  await expect(page.getByTestId('deploy-runner')).toContainText('genet-deploy')
  await expect(page.getByTestId('deploy-clean-tag')).toContainText('自动注入 clean')

  // 平台部署草稿：写一段合法 YAML
  await replaceYaml(page, 'provider: genet\nmodel: Qwen3-32B')

  // 切「已有服务」→ 填 URL（两形态互斥，但各自草稿都在）
  await mode.getByRole('button', { name: '已有服务' }).click()
  const url = page.getByLabel('服务 base URL')
  await expect(url).toBeVisible()
  await url.fill('http://10.0.0.99:8000/v1')
  await expect(page.getByTestId('submit-check-service')).toContainText('已有服务')

  // 切回平台部署：YAML 草稿原样在；再切回已有服务：URL 也原样在
  await mode.getByRole('button', { name: '平台部署' }).click()
  const editor = page.getByLabel('部署描述 YAML')
  await expect(editor).toContainText('model: Qwen3-32B')
  await mode.getByRole('button', { name: '已有服务' }).click()
  await expect(page.getByLabel('服务 base URL')).toHaveValue('http://10.0.0.99:8000/v1')

  // 提交检查 chip：URL 已填 → 服务来源已就绪
  await expect(page.getByTestId('submit-check-service')).toContainText('已就绪')
})

test('④ 提交检查区：服务来源 / Runner 输入 / 校验三枚 chip（#111）', async ({ page }) => {
  await openSubmit(page)

  await expect(page.getByTestId('submit-check-service')).toContainText('平台部署')
  await expect(page.getByTestId('submit-check-runners')).toContainText('1 行')
  await expect(page.getByTestId('submit-check-validate')).toContainText('未校验')

  // 校验通过后 chip 变绿（文案换成「校验通过」）
  await page.getByTestId('validate-task').click()
  await expect(page.getByTestId('submit-check-validate')).toContainText('校验通过', { timeout: 15_000 })

  // sticky：footer 在视口内可达（长页面不用滚到底才看到提交按钮）
  const footer = page.getByTestId('submit-footer')
  const inViewport = await footer.evaluate((el) => {
    const rect = el.getBoundingClientRect()
    return rect.bottom <= window.innerHeight + 1
  })
  expect(inViewport, '提交检查区必须 sticky 在视口内').toBe(true)
})

/* ============================================================ 5. 成功 Task 主动作（#115） */

test('⑤ 成功 Task：结论给出「查看报告」主动作，一步进报告（#115）', async ({ page }) => {
  await page.addInitScript(() => window.localStorage.setItem('argus:mock-role', 'operator'))
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto('/tasks/t-20261105-0006')

  const verdict = page.getByTestId('task-result-verdict')
  await expect(verdict).toBeVisible({ timeout: 20_000 })
  // 结论不再空喊"可以出报告"——主动作直接可点
  await expect(verdict).toContainText('全部 6 个 runner 通过')
  await expect(verdict).not.toContainText('可以出报告')

  const primary = page.getByTestId('result-primary-action')
  await expect(primary).toBeVisible()
  await expect(primary).toHaveText(/查看报告/)
  await primary.click()
  await expect(page).toHaveURL(/\/file\?path=report\.md/, { timeout: 15_000 })

  // 打包下载保留为次级动作
  await page.goBack()
  await expect(page.getByTestId('download-bundle')).toBeVisible()
})

/* ============================================================ 6. 失败 Task 主动作（#116） */

test('⑥ 失败 Task：阶段 / 影响 / 下一步 +「查看失败日志」主动作（#116）', async ({ page }) => {
  await page.addInitScript(() => window.localStorage.setItem('argus:mock-role', 'operator'))
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto('/tasks/t-20261105-0003')

  const verdict = page.getByTestId('task-result-verdict')
  await expect(verdict).toBeVisible({ timeout: 20_000 })

  // 1) 哪个阶段失败（runner 名进结论，不再拿错误码当结论）
  await expect(verdict).toContainText('测试 runner「accuracy-dataset-a」失败')
  // 2) 对后续 runner 的影响（跳过不是 0 也不是成功）
  await expect(verdict).toContainText('跳过 1 个（前序失败，未执行')
  // 3) 下一步
  await expect(verdict).toContainText('下一步：')

  // 主动作：查看失败日志，一步直达失败 runner 的日志
  const primary = page.getByTestId('result-primary-action')
  await expect(primary).toHaveText(/查看失败日志/)
  await primary.click()
  await expect(page).toHaveURL(/file\?path=attempts%2F1%2Frunners%2F3%2Flogs%2Fstdout\.log/, { timeout: 15_000 })

  // 原始错误码保留为诊断信息（可查但不重复占主结论——结论行里不再出现裸码开头）
  await page.goBack()
  const diagnostic = page.getByTestId('task-result-verdict').getByText('诊断信息')
  await expect(diagnostic).toBeVisible()
  await expect(page.getByTestId('task-result-verdict').locator('span.font-mono', { hasText: 'RUNNER_NONZERO_EXIT' })).toHaveCount(1)
})

/* ============================================================ 7. precheck 失败路径（#116） */

test('⑦ 服务不可用：precheck 阶段的文案准确，日志直达 precheck runner（#116）', async ({ page }) => {
  await page.addInitScript(() => window.localStorage.setItem('argus:mock-role', 'operator'))
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto('/tasks/t-20261105-0002')

  const verdict = page.getByTestId('task-result-verdict')
  await expect(verdict).toBeVisible({ timeout: 20_000 })
  await expect(verdict).toContainText('就绪探测 precheck')
  await expect(verdict).toContainText('跳过 2 个（前序失败，未执行')
  await expect(verdict).toContainText('503 / 超时')
  await expect(verdict).toContainText('401 / 403')

  const primary = page.getByTestId('result-primary-action')
  await expect(primary).toHaveText(/查看失败日志/)
  await primary.click()
  await expect(page).toHaveURL(/file\?path=attempts%2F1%2Frunners%2F2%2Flogs%2Fstdout\.log/, { timeout: 15_000 })
})

/* ============================================================ 8. 关键操作文字 ≥13.5px（#117） */

test('⑧ 关键操作文字与表格内容字号达标（#117）', async ({ page }) => {
  await openOverview(page)

  // 提交按钮（关键操作）≥ 13.5px
  const sizes = await page.evaluate(() => {
    const probe = (el: Element | null) => (el ? Number.parseFloat(getComputedStyle(el).fontSize) : null)
    return {
      newTask: probe(document.querySelector('[data-testid="new-task"]')),
      tableCell: probe(document.querySelector('tbody td')),
    }
  })
  expect(sizes.newTask).toBeGreaterThanOrEqual(13.5)
  expect(sizes.tableCell).toBeGreaterThanOrEqual(12) // 表格内容 14 / 元信息 12+
})
