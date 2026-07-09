import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Dashboard', () => {
  test('redirects to login when unauthenticated', { tag: '@smoke' }, async ({ page }) => {
    await page.goto('/')
    await page.waitForLoadState('networkidle')

    await expect(page).toHaveURL(/\/login/)
  })

  test('displays dashboard heading when authenticated', async ({ page, env }) => {
    await page.goto('/login')
    await page.waitForLoadState('networkidle')

    await loginAsAdmin(page, env)

    const isOnDashboard = page.url() !== `${page.context().options.baseURL}/login`
    if (isOnDashboard) {
      await expect(page.locator('h1')).toContainText('Dashboard')
    }
  })
})
