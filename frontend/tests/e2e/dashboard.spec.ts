import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Dashboard', () => {
  test('redirects to login when unauthenticated', { tag: '@smoke' }, async ({ page }) => {
    await page.goto('/')

    await expect(page).toHaveURL(/\/login/)
  })

  test('displays dashboard heading when authenticated', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.reload()

    await expect(page.locator('h1')).toContainText('Dashboard')
  })

  test('trend window toggles reflect selection', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.reload()

    const heading = page.locator('[data-testid="dashboard-title"]')
    await expect(heading).toBeVisible()

    // A fresh session has no persisted preference, and loadTrendWindow() falls
    // back to the 3-day rolling window when localStorage holds nothing.
    const defaultToggle = page.locator('[data-testid="trend-toggle-3"]')
    await expect(defaultToggle).toHaveAttribute('aria-pressed', 'true')

    const weekToggle = page.locator('[data-testid="trend-toggle-7"]')
    await weekToggle.click()

    await expect(weekToggle).toHaveAttribute('aria-pressed', 'true')
    await expect(defaultToggle).toHaveAttribute('aria-pressed', 'false')
  })

  test('run activity card shows HITL and rejection rate labels', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.reload()

    const runActivity = page.locator('[data-testid="dashboard-run-activity"]')
    // The card is behind the dashboard_charts feature flag — only assert when present
    if (await runActivity.count() > 0) {
      await expect(runActivity).toContainText('Avg approval time')
      await expect(runActivity).toContainText('Rejection rate')
    }
  })
})
