import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Admin Sandbox Concurrency', { tag: "@regression" }, () => {
  test('renders the Sandbox Concurrency page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/sandbox-concurrency')
    await expect(page).toHaveURL(/\/admin\/sandbox-concurrency$/)
    await expect(page.locator('h1')).toContainText('Max concurrent Runner runs')
    if (env.name === 'local') {
      await expect(page.getByTestId('admin-sandbox-concurrency-limit')).toBeVisible()
      await expect(page.getByTestId('admin-sandbox-concurrency-save')).toBeVisible()
    }
  })
})
