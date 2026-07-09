import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Admin API Changelog', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/api-changelog')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('API Changelog')
    await expect(page.getByTestId('changelog-title')).toBeVisible()
  })
})

test.describe('Admin Audit Log', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/audit')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Audit Log')
    await expect(page.getByTestId('admin-audit-verify-chain')).toBeVisible()
  })
})

test.describe('Admin My Profile', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/my-profile')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('My Profile')
    await expect(page.getByTestId('my-profile-current-password')).toBeVisible()
  })
})

test.describe('Admin Notification Delivery', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/notification-delivery')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Notification Delivery Log')
    await expect(page.getByTestId('admin-notification-log-title')).toBeVisible()
  })
})

test.describe('Admin Org Settings', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/org')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Organisation Settings')
    await expect(page.getByTestId('org-delete-confirm-input')).toBeVisible()
  })
})

test.describe('Admin Pipelines', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/pipelines')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Pipelines')
    await expect(page.getByTestId('pipeline-list-new-pipeline')).toBeVisible()
  })
})

test.describe('Admin Plugins', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/plugins')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Plugins')
    await expect(page.getByTestId('admin-plugins-refresh')).toBeVisible()
  })
})

test.describe('Admin Team Comparison', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/teams/comparison')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Team Comparison')
    await expect(page.locator('h1')).toBeVisible()
  })
})

test.describe('Admin Triggers', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/triggers')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Triggers')
    await expect(page.getByTestId('settings-triggers-create')).toBeVisible()
  })
})
