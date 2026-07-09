import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Remy Admin Configuration', () => {
  test('page loads with remy configuration sections', async ({ page, env }) => {
    await page.route('**/api/v1/remy/skills*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'rs1', name: 'Code Review', description: 'Automated code review skill', enabled: true, created_at: '2025-06-01T10:00:00Z' }, { id: 'rs2', name: 'Documentation', description: 'Generates documentation from code', enabled: false, created_at: '2025-06-02T10:00:00Z' }], total: 2 }) })
    })
    await page.route('**/api/v1/remy/config*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ providers: [{ name: 'openai', api_key_configured: true, models: ['gpt-4', 'gpt-3.5-turbo'] }], custom_backends: [] }) })
    })
    await loginAsAdmin(page, env)

    await page.goto('/admin/remy')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText('Remy Configuration')
    await expect(page.getByTestId('remy-providers')).toBeVisible()
    await expect(page.getByTestId('remy-custom-backends')).toBeVisible()
  })
})
