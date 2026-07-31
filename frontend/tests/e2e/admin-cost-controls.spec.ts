import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Admin Spend Limits', { tag: "@regression" }, () => {
  test('page loads with correct heading', { tag: "@regression" }, async ({ page, env }) => {
    if (env.name === 'local') {
      await page.route('**/api/v1/admin/costs/limits*', (route) => {
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ organisation_id: 'test-org', org_daily_spend_limit: 100, team_limits: [] }) })
      })
    }
    await loginAsAdmin(page, env)
    await page.goto('/admin/costs/limits', { timeout: 60000 })
    await expect(page.locator('h1')).toContainText('Spend Limits')
    await expect(
      page.getByTestId('admin-spend-limits-org-limit').or(page.getByTestId('feature-gate-disabled'))
    ).toBeVisible({ timeout: 15000 })
  })
})

test.describe('Admin Cost Controls', { tag: "@regression" }, () => {
  test('page loads with correct heading', { tag: "@regression" }, async ({ page, env }) => {
    if (env.name === 'local') {
      await page.route('**/api/v1/admin/costs*', (route) => {
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ period: 'month', group_by: 'team', items: [] }) })
      })
      await page.route('**/api/v1/admin/costs/controls*', (route) => {
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ teams: [], budget: 1000, currency: 'USD', billing_period: 'monthly', alert_thresholds: [50, 75, 90], circuit_breaker_enabled: false }) })
      })
      await page.route('**/api/v1/admin/costs/limits*', (route) => {
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ organisation_id: 'test-org', org_daily_spend_limit: 100, team_limits: [] }) })
      })
    }
    await loginAsAdmin(page, env)
    await page.goto('/admin/costs/controls', { timeout: 60000 })
    await expect(page.locator('h1')).toContainText('Cost Controls')
    await expect(
      page.getByTestId('cc-total-spend').or(page.getByTestId('feature-gate-disabled'))
    ).toBeVisible({ timeout: 15000 })
  })
})
