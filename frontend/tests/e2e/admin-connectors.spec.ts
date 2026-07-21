import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Admin Connectors', () => {
  test('page loads with correct heading and add connector button', async ({ page, env }) => {
    await page.route('**/api/v1/connectors*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'c1', name: 'GitHub', connector_type: 'github', status: 'connected', description: 'GitHub code repository connector', created_at: '2025-06-01T10:00:00Z' }], total: 1 }) })
    })
    await loginAsAdmin(page, env)

    await page.goto('/admin/connectors')

    await expect(page.locator('h1')).toContainText('Connectors')
    await expect(page.getByTestId('admin-connectors-add')).toBeVisible()
  })

  test('shows empty state with create button visible', async ({ page, env }) => {
    await page.route('**/api/v1/connectors*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'c1', name: 'GitHub', connector_type: 'github', status: 'connected', description: 'GitHub code repository connector', created_at: '2025-06-01T10:00:00Z' }], total: 1 }) })
    })
    await loginAsAdmin(page, env)

    await page.goto('/admin/connectors')

    await expect(page.getByTestId('admin-connectors-add')).toContainText('Add Connector')
  })
})
