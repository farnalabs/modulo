import { test, expect, loginAsAdmin } from './setup/fixtures'

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
