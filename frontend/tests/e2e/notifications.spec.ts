import { test, expect } from '@playwright/test'

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.waitForLoadState('networkidle')
  await page.fill('input[type="text"]', 'admin@example.com')
  await page.fill('input[type="password"]', 'password123')
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 5000 }).catch(() => {})
}

test.describe('Notifications', () => {
  test('notifications page loads', async ({ page }) => {
    await login(page)
    await page.goto('/notifications')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText(/Notification/i)
  })

  test('notification list renders elements', async ({ page }) => {
    await login(page)
    await page.route('**/api/v1/notifications*', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            {
              id: 'n1',
              title: 'Pipeline run completed',
              message: 'My Pipeline finished successfully',
              type: 'success',
              read: false,
              created_at: '2025-06-01T10:00:00Z',
            },
            {
              id: 'n2',
              title: 'HITL review requested',
              message: 'Approval needed for production deploy',
              type: 'warning',
              read: true,
              created_at: '2025-06-01T11:00:00Z',
            },
          ],
          total: 2,
        }),
      })
    })

    await page.goto('/notifications')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('text=Pipeline run completed')).toBeVisible()
    await expect(page.locator('text=HITL review requested')).toBeVisible()
  })
})
