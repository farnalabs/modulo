import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Admin Cost Components', { tag: "@regression" }, () => {
  test('renders the Cost Components page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/costs/components')
    await expect(page).toHaveURL(/\/admin\/costs\/components$/)
    await expect(page.locator('h1')).toContainText('Cost Components')
    await expect(page.getByTestId('cost-components-add')).toBeVisible()
    if (env.name === 'local') {
      await expect(page.getByText('No cost components configured.')).toBeVisible()
    }
  })
})
