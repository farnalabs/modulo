import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Admin Environments', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/environments')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Environment Profiles')
    await expect(page.getByTestId('admin-envprofiles-add')).toBeVisible()
  })
})

test.describe('Admin Node Categories', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/node-categories')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Node Categories')
    await expect(page.getByTestId('admin-node-categories-add')).toBeVisible()
  })
})

test.describe('Admin Run Retention', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/run-retention')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Run Retention')
    await expect(page.getByTestId('admin-run-retention-days')).toBeVisible()
  })
})
