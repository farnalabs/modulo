import { test, expect } from './setup/fixtures'

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.waitForLoadState('networkidle')
  await page.fill('input[type="text"]', 'admin@example.com')
  await page.fill('input[type="password"]', 'password123')
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 5000 }).catch(() => {})
}

test.describe('Error Tracking', () => {
  test('error dashboard page loads', async ({ page }) => {
    await login(page)
    await page.goto('/admin/errors')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText(/Error/i)
  })

  test('error dashboard shows UI elements', async ({ page }) => {
    await login(page)
    await page.route('**/api/v1/admin/errors*', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            {
              id: 'e1',
              message: 'Connection timeout',
              count: 15,
              last_seen: '2025-06-01T12:00:00Z',
              status: 'unresolved',
            },
          ],
          total: 1,
        }),
      })
    })

    await page.goto('/admin/errors')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('text=Connection timeout')).toBeVisible()
  })
})
