import { test, expect } from '@playwright/test'

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.waitForLoadState('networkidle')
  await page.fill('input[type="text"]', 'admin@example.com')
  await page.fill('input[type="password"]', 'password123')
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 5000 }).catch(() => {})
}

test.describe('Settings Email', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/settings/email')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Email')
    await expect(page.getByTestId('page-settings-email')).toBeVisible()
  })
})

test.describe('Settings Error Forwarders', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/settings/error-forwarders')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Error Forwarders')
    await expect(page.getByTestId('page-error-forwarders')).toBeVisible()
  })
})

test.describe('Settings Observability', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/settings/observability')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Observability')
    await expect(page.getByTestId('page-settings-observability')).toBeVisible()
  })
})
