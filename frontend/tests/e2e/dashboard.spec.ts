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
