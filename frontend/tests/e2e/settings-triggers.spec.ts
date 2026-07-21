import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Settings Triggers', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await page.route('**/api/v1/triggers*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'tr1', name: 'Deploy Webhook', trigger_type: 'webhook', pipeline_id: 'p1', status: 'active', created_at: '2025-06-01T10:00:00Z' }], total: 1 }) })
    })
    await loginAsAdmin(page, env)

    await page.goto('/settings/triggers')

    await expect(page.locator('h1')).toContainText('Triggers')
  })

  test('shows create trigger button', async ({ page, env }) => {
    await page.route('**/api/v1/triggers*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'tr1', name: 'Deploy Webhook', trigger_type: 'webhook', pipeline_id: 'p1', status: 'active', created_at: '2025-06-01T10:00:00Z' }], total: 1 }) })
    })
    await loginAsAdmin(page, env)

    await page.goto('/settings/triggers')

    await expect(page.getByTestId('settings-triggers-create')).toBeVisible()
  })
})
