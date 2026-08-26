import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Admin Environments', { tag: "@regression" }, () => {
  test('renders the Environment Profiles page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/environments')
    await expect(page.locator('h1')).toContainText('Environment Profiles')
    if (env.name === 'local') {
      await expect(page.getByTestId('admin-envprofiles-add')).toBeVisible()
    }
  })
})

test.describe('Admin Node Categories', { tag: "@regression" }, () => {
  test('renders the Node Categories page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/node-categories')
    await expect(page.locator('h1')).toContainText('Node Categories')
    if (env.name === 'local') {
      await expect(page.getByTestId('admin-node-categories-add')).toBeVisible()
    }
  })
})

test.describe('Admin Run Retention', { tag: "@regression" }, () => {
  test('renders the Run Retention page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/run-retention')
    await expect(page.locator('h1')).toContainText('Run Retention')
    if (env.name === 'local') {
      await expect(page.getByTestId('admin-run-retention-refresh')).toBeVisible()
    }
  })
})
