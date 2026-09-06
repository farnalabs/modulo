import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Admin Housekeeping', { tag: "@regression" }, () => {
  test('renders the Housekeeping page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/housekeeping')
    await expect(page).toHaveURL(/\/admin\/housekeeping$/)
    await expect(page.locator('h1')).toContainText('Housekeeping')
    await expect(page.getByTestId('hk-refresh')).toBeVisible()
    await expect(page.getByTestId('hk-checkpoint-retention')).toBeVisible()
    if (env.name === 'local') {
      await expect(page.getByTestId('hk-empty')).toBeVisible()
      await expect(page.getByText('All Clean!')).toBeVisible()
      await expect(page.getByTestId('hk-ckpt-max-age')).toBeVisible()
      await expect(page.getByTestId('hk-ckpt-purge')).toBeVisible()
    }
  })
})
