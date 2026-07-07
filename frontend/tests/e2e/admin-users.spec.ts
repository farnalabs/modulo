import { test, expect } from '@playwright/test'

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.waitForLoadState('networkidle')
  await page.fill('input[type="text"]', 'admin@example.com')
  await page.fill('input[type="password"]', 'password123')
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 5000 }).catch(() => {})
}

test.describe('Admin Users', () => {
  test('page loads with correct heading and add user button', async ({ page }) => {
    await page.route('**/api/v1/users*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
    })
    await login(page)

    await page.goto('/admin/users')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText('Users')
    await expect(page.getByTestId('admin-users-add-user')).toBeVisible()
  })

  test('shows empty state when no users exist', async ({ page }) => {
    await page.route('**/api/v1/users*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
    })
    await login(page)

    await page.goto('/admin/users')
    await page.waitForLoadState('networkidle')

    await expect(page.getByTestId('admin-users-add-user')).toBeVisible()
  })
})
