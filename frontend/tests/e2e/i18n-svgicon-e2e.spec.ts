import { test, expect, loginAsAdmin, loginForSystemRoute } from './setup/fixtures'

test.describe('i18n Keys & SvgIcon Regression', () => {
  test('sidebar shows "Runners" not raw key', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/admin/runners/profiles')

    // CONFIGURE group button - click to expand if collapsed
    const configureGroup = page.locator('button.sidebar-group-header', { hasText: 'CONFIGURE' }).first()
    await configureGroup.click()

    // Check that the sidebar link is "Runners" not "nav.admin-runners-profiles"
    const runnersLink = page.locator('a.sidebar-link', { hasText: 'Runners' }).first()
    await expect(runnersLink).toBeVisible()

    const rawKeyLink = page.locator('a.sidebar-link', { hasText: 'nav.admin-runners-profiles' })
    await expect(rawKeyLink).toHaveCount(0)
  })

  test('no SvgIcon warnings for Mail or File icons', { tag: "@regression" }, async ({ page, env }) => {
    const warnings: string[] = []
    page.on('console', (msg) => {
      if (msg.type() === 'warn' && msg.text().includes('SvgIcon: unknown icon')) {
        warnings.push(msg.text())
      }
    })

    // /settings/email is organisation-scoped (the API lives under
    // /api/v1/admin/org/{org_id}) and now sits in the ADMIN sidebar group
    // (#2455), so ordinary org admins can render it. Assert the surface was
    // actually reached rather than letting the test pass without exercising
    // the page whose Mail/File icons it means to check (FAR-1919).
    await loginForSystemRoute(page, env)
    await page.goto('/settings/email')
    await expect(page.locator('h1')).toContainText('Email Settings')

    await page.goto('/dashboard')

    expect(warnings.filter(w => w.includes('"Mail"') || w.includes('"File"'))).toEqual([])
  })
})
