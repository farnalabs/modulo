import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Admin Users', () => {
  test('page loads with correct heading and add user button', async ({ page, env }) => {
    await page.route('**/api/v1/users*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'u1', email: 'alice@example.com', display_name: 'Alice', role: 'admin', status: 'active', created_at: '2025-01-15T10:00:00Z' }, { id: 'u2', email: 'bob@example.com', display_name: 'Bob', role: 'member', status: 'active', created_at: '2025-02-01T10:00:00Z' }], total: 2 }) })
    })
    await loginAsAdmin(page, env)

    await page.goto('/admin/users')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText('Users')
    await expect(page.getByTestId('admin-users-add-user')).toBeVisible()
  })

  test('shows empty state when no users exist', async ({ page, env }) => {
    await page.route('**/api/v1/users*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'u1', email: 'alice@example.com', display_name: 'Alice', role: 'admin', status: 'active', created_at: '2025-01-15T10:00:00Z' }, { id: 'u2', email: 'bob@example.com', display_name: 'Bob', role: 'member', status: 'active', created_at: '2025-02-01T10:00:00Z' }], total: 2 }) })
    })
    await loginAsAdmin(page, env)

    await page.goto('/admin/users')
    await page.waitForLoadState('networkidle')

    await expect(page.getByTestId('admin-users-add-user')).toBeVisible()
  })
})
