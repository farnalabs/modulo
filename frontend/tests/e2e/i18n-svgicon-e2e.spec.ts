import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('i18n Keys & SvgIcon Regression', () => {
  test('sidebar shows "Environment Profiles" not raw key', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/environment-profiles')

    // CONFIGURE group button - click to expand if collapsed
    const configureGroup = page.locator('button.sidebar-group-header', { hasText: 'CONFIGURE' }).first()
    await configureGroup.click()

    // Check that the sidebar link is "Environment Profiles" not "nav.environment-profiles"
    const envProfileLink = page.locator('a.sidebar-link', { hasText: 'Environment Profiles' }).first()
    await expect(envProfileLink).toBeVisible()

    const rawKeyLink = page.locator('a.sidebar-link', { hasText: 'nav.environment-profiles' })
    await expect(rawKeyLink).toHaveCount(0)
  })

  test('no SvgIcon warnings for Mail or File icons', { tag: "@regression" }, async ({ page, env }) => {
    const warnings: string[] = []
    page.on('console', (msg) => {
      if (msg.type() === 'warn' && msg.text().includes('SvgIcon: unknown icon')) {
        warnings.push(msg.text())
      }
    })

    await loginAsAdmin(page, env)
    await page.goto('/settings/email')

    await page.goto('/dashboard')

    expect(warnings.filter(w => w.includes('"Mail"') || w.includes('"File"'))).toEqual([])
  })
})
