import { test, expect, loginAsAdmin } from './setup/fixtures'

const emptyAnalyticsResponse = {
  group_by: 'day',
  buckets: [],
  facts_stale: false,
}

test.describe('Analytics', { tag: "@regression" }, () => {
  test('renders the Analytics page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    if (env.name === 'local') {
      await page.route('**/api/v1/analytics/query*', (route) => {
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(emptyAnalyticsResponse) })
      })
    }
    await page.goto('/analytics')
    await expect(page).toHaveURL(/\/analytics$/)
    await expect(page.locator('h1')).toContainText('Analytics')
    await expect(page.getByTestId('analytics-view')).toBeVisible()
    if (env.name === 'local') {
      await expect(page.getByTestId('analytics-filter-bar')).toBeVisible()
      await expect(page.getByTestId('analytics-empty-state')).toBeVisible()
    }
  })

  test('shows the not-enabled notice when the plan lacks analytics', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Mocks the analytics endpoint with a 402 — only runs locally')
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/analytics/query*', (route) => {
      route.fulfill({
        status: 402,
        contentType: 'application/json',
        body: JSON.stringify({
          type: 'urn:problem:modulo:feature_required',
          title: 'Feature Not Available',
          status: 402,
          detail: 'Analytics is not enabled for your plan.',
        }),
      })
    })
    await page.goto('/analytics')
    await expect(page.locator('h1')).toContainText('Analytics')
    await expect(page.getByTestId('analytics-not-enabled')).toBeVisible()
    await expect(page.getByText('Analytics is not enabled for your workspace')).toBeVisible()
  })
})
