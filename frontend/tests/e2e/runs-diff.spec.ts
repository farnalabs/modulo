import { test, expect, loginAsAdmin, isDevModeTarget } from './setup/fixtures'

test.describe('Output Diff', { tag: "@regression" }, () => {
  test('page loads with correct heading', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(!isDevModeTarget(env), 'Route is dev-mode-gated (private_preview); only runs on a dev-mode target')
    await page.route('**/api/v1/runs*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'r1', pipeline_id: 'p1', pipeline_name: 'CI Pipeline', status: 'completed', created_at: '2025-06-10T10:00:00Z', duration_seconds: 45 }, { id: 'r2', pipeline_id: 'p1', pipeline_name: 'CI Pipeline', status: 'completed', created_at: '2025-06-11T10:00:00Z', duration_seconds: 52 }], total: 2 }) })
    })
    await loginAsAdmin(page, env)
    await page.goto('/runs/diff')
    await expect(page.locator('h1')).toContainText('Agent Output Diff')
    await expect(page.getByTestId('diff-recent-runs-a')).toBeVisible()
  })
})
