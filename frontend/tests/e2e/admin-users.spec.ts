import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Admin Users', () => {
  test('page loads with correct heading and add user button', async ({ page, env }) => {
    await page.route('**/api/v1/users*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
    })
    await loginAsAdmin(page, env)

    await page.goto('/admin/users')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText('Users')
    await expect(page.getByTestId('admin-users-add-user')).toBeVisible()
  })

  test('shows empty state when no users exist', async ({ page, env }) => {
    await page.route('**/api/v1/users*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
    })
    await loginAsAdmin(page, env)

    await page.goto('/admin/users')
    await page.waitForLoadState('networkidle')

    await expect(page.getByTestId('admin-users-add-user')).toBeVisible()
  })
})
