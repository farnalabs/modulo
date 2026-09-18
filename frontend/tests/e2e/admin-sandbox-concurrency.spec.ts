import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Runners Concurrency', { tag: "@regression" }, () => {
  test('renders the Concurrency tab (legacy sandbox-concurrency route redirects)', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/sandbox-concurrency')
    await expect(page).toHaveURL(/\/admin\/runners\/concurrency$/)
    await expect(page.locator('h1')).toContainText('Runners')
    if (env.name === 'local') {
      await expect(page.getByTestId('admin-sandbox-concurrency-limit')).toBeVisible()
      await expect(page.getByTestId('admin-sandbox-concurrency-save')).toBeVisible()
      await expect(page.getByTestId('runner-concurrency-effective')).toBeVisible()
    }
  })
})
