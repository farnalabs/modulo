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
              sample_message: 'Connection timeout',
              level_peak: 'error',
              count: 15,
              first_seen: '2025-06-01T12:00:00Z',
              last_seen: '2025-06-01T12:00:00Z',
              status: 'new',
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
