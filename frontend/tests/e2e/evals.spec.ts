import { test, expect } from './setup/fixtures'

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.waitForLoadState('networkidle')
  await page.fill('input[type="text"]', 'admin@example.com')
  await page.fill('input[type="password"]', 'password123')
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 5000 }).catch(() => {})
}

test.describe('Evals', () => {
  test('eval editor page loads', async ({ page }) => {
    await login(page)
    await page.goto('/evals/editor')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText(/Eval|Editor/i)
  })

  test('eval proposals page loads', async ({ page }) => {
    await login(page)
    await page.goto('/evals/proposals')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText(/Proposal/i)
  })

  test('variants compare page loads', async ({ page }) => {
    await login(page)
    await page.goto('/variants/compare')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText(/Variant|Compare/i)
  })
})
