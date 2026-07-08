import { test, expect } from './setup/fixtures'

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.waitForLoadState('networkidle')
  await page.fill('input[type="text"]', 'admin@example.com')
  await page.fill('input[type="password"]', 'password123')
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 5000 }).catch(() => {})
}

test.describe('Admin API Changelog', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/admin/api-changelog')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('API Changelog')
    await expect(page.getByTestId('page-api-changelog')).toBeVisible()
  })
})

test.describe('Admin Audit Log', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/admin/audit')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Audit Log')
    await expect(page.getByTestId('page-audit-log')).toBeVisible()
  })
})

test.describe('Admin My Profile', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/admin/my-profile')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('My Profile')
    await expect(page.getByTestId('page-my-profile')).toBeVisible()
  })
})

test.describe('Admin Notification Delivery', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/admin/notification-delivery')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Notification Log')
    await expect(page.getByTestId('page-notification-delivery')).toBeVisible()
  })
})

test.describe('Admin Org Settings', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/admin/org')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Org Settings')
    await expect(page.getByTestId('page-org-settings')).toBeVisible()
  })
})

test.describe('Admin Pipelines', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/admin/pipelines')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Admin Pipelines')
    await expect(page.getByTestId('page-admin-pipelines')).toBeVisible()
  })
})

test.describe('Admin Plugins', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/admin/plugins')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Plugins')
    await expect(page.getByTestId('page-plugins')).toBeVisible()
  })
})

test.describe('Admin Team Comparison', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/admin/teams/comparison')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Team Comparison')
    await expect(page.getByTestId('page-team-comparison')).toBeVisible()
  })
})

test.describe('Admin Triggers', () => {
  test('page loads with correct heading', async ({ page }) => {
    await login(page)
    await page.goto('/admin/triggers')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Admin Triggers')
    await expect(page.getByTestId('page-admin-triggers')).toBeVisible()
  })
})
