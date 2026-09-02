import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Environment Profiles', { tag: "@regression" }, () => {
  test('renders the Environment Profiles page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/environment-profiles')
    await expect(page.locator('h1')).toContainText('Environment Profiles')
    if (env.name === 'local') {
      await expect(page.getByTestId('envprofile-list-new')).toBeVisible()
    }
  })

  test('redirects the retired /admin/environments path', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/environments')
    await expect(page).toHaveURL(/\/environment-profiles$/)
    await expect(page.locator('h1')).toContainText('Environment Profiles')
  })
})
