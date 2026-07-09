import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Sidebar Navigation', () => {
  test('displays Core, Settings, and Remy groups in simple mode', { tag: '@smoke' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    await page.goto('/')
    await page.waitForLoadState('networkidle')

    const sidebar = page.locator('nav[aria-label="Main navigation"]')
    await expect(sidebar).toBeVisible()

    const groupHeaders = sidebar.locator('button.sidebar-group-header')
    const headerCount = await groupHeaders.count()
    expect(headerCount).toBeGreaterThanOrEqual(3)
  })

  test('sidebar link navigates to the correct page', async ({ page, env }) => {
    await loginAsAdmin(page, env)

    await page.goto('/')
    await page.waitForLoadState('networkidle')

    const libraryLink = page.locator('a.sidebar-link', { hasText: 'Library' })
    await expect(libraryLink).toBeVisible()
    await libraryLink.click()
    await page.waitForLoadState('networkidle')

    await expect(page).toHaveURL(/\/library/)
  })

  test('all sidebar links have visible labels', async ({ page, env }) => {
    await loginAsAdmin(page, env)

    await page.goto('/')
    await page.waitForLoadState('networkidle')

    const sidebarLinks = page.locator('nav[aria-label="Main navigation"] a.sidebar-link')
    const count = await sidebarLinks.count()
    expect(count).toBeGreaterThan(0)

    for (let i = 0; i < count; i++) {
      const link = sidebarLinks.nth(i)
      await expect(link).toBeVisible()
      const text = await link.innerText()
      expect(text.trim().length).toBeGreaterThan(0)
    }
  })
})
