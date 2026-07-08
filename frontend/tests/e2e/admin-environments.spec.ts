import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Admin Environments', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/environments')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Environments')
    await expect(page.getByTestId('page-environments')).toBeVisible()
  })
})

test.describe('Admin Node Categories', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/node-categories')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Node Categories')
    await expect(page.getByTestId('page-node-categories')).toBeVisible()
  })
})

test.describe('Admin Run Retention', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/run-retention')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Run Retention')
    await expect(page.getByTestId('page-run-retention')).toBeVisible()
  })
})
