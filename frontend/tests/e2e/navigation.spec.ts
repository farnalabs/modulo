import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Navigation Flow', () => {
  test('navigates from Dashboard to Pipelines', async ({ page, env }) => {
    await loginAsAdmin(page, env)

    await page.goto('/')
    await page.waitForLoadState('networkidle')

    const pipelinesLink = page.locator('a.sidebar-link', { hasText: 'My Pipelines' }).first()
    await expect(pipelinesLink).toBeVisible()
    await pipelinesLink.click()
    await page.waitForLoadState('networkidle')

    await expect(page).toHaveURL(/\/pipelines/)
    await expect(page.locator('h1')).toContainText('Pipelines')
  })

  test('current page indicator is shown on active sidebar link', async ({ page, env }) => {
    await loginAsAdmin(page, env)

    await page.goto('/')
    await page.waitForLoadState('networkidle')

    const dashboardLink = page.locator('a.sidebar-link.active').first()
    await expect(dashboardLink).toHaveCount(1)
    await expect(dashboardLink).toHaveAttribute('aria-current', 'page')
  })

  test('browser back navigation works between pages', async ({ page, env }) => {
    await loginAsAdmin(page, env)

    await page.goto('/')
    await page.waitForLoadState('networkidle')

    await page.goto('/library')
    await page.waitForLoadState('networkidle')
    await expect(page).toHaveURL(/\/library/)

    await page.goBack()
    await page.waitForLoadState('networkidle')
    await expect(page).toHaveURL(/\/$|^[^\/]+$/)
  })
})
