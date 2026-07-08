import { test, expect } from './setup/fixtures'

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.waitForLoadState('networkidle')
  await page.fill('input[type="text"]', 'admin@example.com')
  await page.fill('input[type="password"]', 'password123')
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 5000 }).catch(() => {})
}

test.describe('Stages Board', () => {
  test('stages board page loads', async ({ page }) => {
    await login(page)
    await page.goto('/stages')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText(/Stage|Board/i)
  })

  test('stages board shows columns and elements', async ({ page }) => {
    await login(page)
    await page.route('**/api/v1/stages*', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            { id: 's1', name: 'Development', runs_count: 3 },
            { id: 's2', name: 'Review', runs_count: 5 },
            { id: 's3', name: 'Production', runs_count: 1 },
          ],
          total: 3,
        }),
      })
    })

    await page.goto('/stages')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('text=Development').or(page.locator('text=Review')).or(page.locator('text=Production'))).toBeVisible()
  })
})
