import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Schemas Page', { tag: "@regression" }, () => {
  test('Browse tab is active by default', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    await page.goto('/schemas')

    await expect(page.locator('h1')).toContainText('Schemas')
    const tabs = page.locator('nav[aria-label="Section navigation"] a')
    await expect(tabs).toHaveCount(3)
    await expect(tabs.nth(0)).toContainText('Browse')
    await expect(tabs.nth(1)).toContainText('Editor')
    await expect(tabs.nth(2)).toContainText('Infer')
  })

  test('navigates to Infer tab', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    await page.goto('/schemas/infer')

    await expect(page).toHaveURL(/\/schemas\/infer/)
    const activeTab = page.locator('nav[aria-label="Section navigation"] a.active')
    await expect(activeTab).toContainText('Infer')
  })

  test('navigates to Editor tab', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    await page.goto('/schemas/editor')

    await expect(page).toHaveURL(/\/schemas\/editor/)
    const activeTab = page.locator('nav[aria-label="Section navigation"] a.active')
    await expect(activeTab).toContainText('Editor')
  })
})
