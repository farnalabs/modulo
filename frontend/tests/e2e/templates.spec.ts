import { test, expect, loginAsAdmin } from './setup/fixtures'

// /templates is a legacy alias: the router redirects it to /library (FAR-638,
// batch A). Pin the redirect so the route never rots into a 404/blank page.
test.describe('Templates Route', { tag: '@regression' }, () => {
  test('redirects /templates to the library page', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    await page.goto('/templates')

    await expect(page).toHaveURL(/\/library/)
    await expect(page.locator('h1')).toContainText('Library')
  })
})
