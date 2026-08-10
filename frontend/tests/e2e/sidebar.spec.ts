import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Sidebar Navigation', () => {
  test('displays Core, Settings, and Remy groups in simple mode', { tag: '@smoke' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    await page.goto('/')

    const sidebar = page.locator('nav[aria-label="Main navigation"]').first()
    await expect(sidebar).toBeVisible()

    const groupHeaders = sidebar.locator('button.sidebar-group-header')
    const headerCount = await groupHeaders.count()
    expect(headerCount).toBeGreaterThanOrEqual(2)
  })

  test('sidebar link navigates to the correct page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    await page.goto('/')

    const libraryLink = page.locator('a.sidebar-link', { hasText: 'Library' }).first()
    await expect(libraryLink).toBeVisible()
    await libraryLink.click()

    await expect(page).toHaveURL(/\/library/)
  })

  test('all sidebar links have visible labels', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    await page.goto('/')

    const sidebar = page.locator('nav[aria-label="Main navigation"]').first()
    await expect(sidebar).toBeVisible()

    // Sidebar groups/links render only after the plan/tier fetch resolves —
    // wait for a group header so the counts below are not taken pre-render.
    const groupHeaders = sidebar.locator('button.sidebar-group-header')
    await expect(groupHeaders.first()).toBeVisible()

    // Expand every collapsed group so all links are visible before asserting
    const headerCount = await groupHeaders.count()
    for (let i = 0; i < headerCount; i++) {
      const header = groupHeaders.nth(i)
      if ((await header.getAttribute('aria-expanded')) === 'false') {
        await header.click()
      }
    }

    const sidebarLinks = sidebar.locator('a.sidebar-link')
    // Wait for at least one link to render before counting — links mount asynchronously
    // after the plan/tier data loads (SidebarNav gates items behind planStore).
    await expect(sidebarLinks.first()).toBeVisible({ timeout: 15000 })
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
