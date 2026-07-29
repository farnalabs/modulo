import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Admin Environments', { tag: "@regression" }, () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/environments')
    await expect(page.locator('h1')).toBeVisible()
    if (env.name === 'local') {
      await expect(page.getByTestId('admin-envprofiles-add')).toBeVisible()
    }
  })
})

test.describe('Admin Node Categories', { tag: "@regression" }, () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/node-categories')
    await expect(page.locator('h1')).toBeVisible()
    if (env.name === 'local') {
      await expect(page.getByTestId('admin-node-categories-add')).toBeVisible()
    }
  })
})

test.describe('Admin Run Retention', { tag: "@regression" }, () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/run-retention')
    await expect(page.locator('h1')).toBeVisible()
    if (env.name === 'local') {
      await expect(page.getByTestId('admin-run-retention-days')).toBeVisible()
    }
  })
})
