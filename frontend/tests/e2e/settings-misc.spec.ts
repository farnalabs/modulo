import { test, expect } from './setup/fixtures'

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.waitForLoadState('networkidle')
  await page.fill('input[type="text"]', 'admin@example.com')
  await page.fill('input[type="password"]', 'password123')
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 5000 }).catch(() => {})
}

test.describe('Settings HITL Review', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/settings/hitl-review')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('HITL Review')
    await expect(page.getByTestId('page-hitl-review')).toBeVisible()
  })
})

test.describe('Settings Browser Monitoring', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/settings/monitoring')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Browser Monitoring')
    await expect(page.getByTestId('page-monitoring')).toBeVisible()
  })
})

test.describe('Settings Rate Limits', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/settings/rate-limits')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Rate Limits')
    await expect(page.getByTestId('page-rate-limits')).toBeVisible()
  })
})

test.describe('Settings Remy Skills', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/settings/remy')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Remy Skills')
    await expect(page.getByTestId('page-remy-skills')).toBeVisible()
  })
})

test.describe('Settings Runtime Config', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/settings/runtime-config')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Runtime Config')
    await expect(page.getByTestId('page-runtime-config')).toBeVisible()
  })
})
