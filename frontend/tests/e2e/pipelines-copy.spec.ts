import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Copy Pipeline', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await page.route('**/api/v1/pipelines/templates*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'tp1', name: 'CI/CD Pipeline', description: 'Standard CI/CD workflow template', category: 'ci-cd', created_at: '2025-06-01T10:00:00Z' }], total: 1 }) })
    })
    await loginAsAdmin(page, env)
    await page.goto('/pipelines/copy')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Copy Pipeline')
    await expect(page.getByTestId('copy-wizard-step-indicator')).toBeVisible()
  })
})
