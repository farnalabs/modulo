import { test, expect } from './setup/fixtures'

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.waitForLoadState('networkidle')
  await page.fill('input[type="text"]', 'admin@example.com')
  await page.fill('input[type="password"]', 'password123')
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 5000 }).catch(() => {})
}

test.describe('Admin Environments', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/admin/environments')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Environments')
    await expect(page.getByTestId('page-environments')).toBeVisible()
  })
})

test.describe('Admin Node Categories', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/admin/node-categories')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Node Categories')
    await expect(page.getByTestId('page-node-categories')).toBeVisible()
  })
})

test.describe('Admin Run Retention', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/admin/run-retention')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Run Retention')
    await expect(page.getByTestId('page-run-retention')).toBeVisible()
  })
})
