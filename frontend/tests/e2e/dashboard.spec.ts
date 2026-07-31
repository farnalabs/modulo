import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Dashboard', () => {
  test('redirects to login when unauthenticated', { tag: '@smoke' }, async ({ page }) => {
    await page.goto('/')

    await expect(page).toHaveURL(/\/login/)
  })

  test('displays dashboard heading when authenticated', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.reload()

    await expect(page.locator('h1')).toContainText('Dashboard')
  })
})
