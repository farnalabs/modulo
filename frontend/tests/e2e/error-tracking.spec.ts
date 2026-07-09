import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Error Tracking', () => {
  test('error dashboard page loads', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/errors')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText(/Error/i)
  })

  test('error dashboard shows UI elements', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/errors*', (route) => {
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
