import { test, expect } from '@playwright/test'

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.waitForLoadState('networkidle')
  await page.fill('input[type="text"]', 'admin@example.com')
  await page.fill('input[type="password"]', 'password123')
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 5000 }).catch(() => {})
}

test.describe('Output Diff', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/runs/diff')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Output Diff')
    await expect(page.getByTestId('page-output-diff')).toBeVisible()
  })
})
