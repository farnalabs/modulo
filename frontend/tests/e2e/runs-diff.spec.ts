import { test, expect, loginAsAdmin, isDevModeTarget } from './setup/fixtures'

test.describe('Output Diff', { tag: "@regression" }, () => {
  test('page loads with correct heading', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(!isDevModeTarget(env), 'Route is dev-mode-gated (private_preview); only runs on a dev-mode target')
    await page.route('**/api/v1/dashboard/summary', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ recent_runs: [{ id: 'r1', pipeline_name: 'CI Pipeline', status: 'completed', created_at: '2025-06-10T10:00:00Z' }, { id: 'r2', pipeline_name: 'CI Pipeline', status: 'completed', created_at: '2025-06-11T10:00:00Z' }] }) })
    })
    await loginAsAdmin(page, env)
    await page.goto('/runs/diff')
    await expect(page.locator('h1')).toContainText('Agent Output Diff')
    await expect(page.getByTestId('diff-recent-runs-a')).toBeVisible()
  })
})
