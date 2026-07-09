import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Dashboard', () => {
  test('redirects to login when unauthenticated', { tag: '@smoke' }, async ({ page }) => {
    await page.goto('/')
    await page.waitForLoadState('networkidle')

    await expect(page).toHaveURL(/\/login/)
  })

  test('displays dashboard heading when authenticated', async ({ page, env }) => {
    await loginAsAdmin(page, env)

    await expect(page.locator('h1')).toContainText('Dashboard')
  })
})
