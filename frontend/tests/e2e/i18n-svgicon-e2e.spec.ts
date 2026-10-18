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

    // /settings/email lives in the SYSTEM sidebar group (FAR-938), which the
    // router gates on is_system_admin. `loginAsAdmin` issues a plain org-admin
    // identity, so the route redirected to the Dashboard and this test passed
    // vacuously — it never rendered the page whose Mail/File icons it means to
    // check (FAR-1919). Use the system-route identity and assert the surface
    // was actually reached.
    const systemAdmin = await loginForSystemRoute(page, env)
    await page.goto('/settings/email')
    if (systemAdmin) {
      await expect(page.locator('h1')).toContainText('Email Settings')
    } else {
      // The staging/prod e2e identity is a regular org admin, so the SYSTEM
      // route redirects — assert the documented redirect rather than letting
      // the test pass without exercising anything.
      await expect(page).toHaveURL(/\/$/)
    }

    await page.goto('/dashboard')

    expect(warnings.filter(w => w.includes('"Mail"') || w.includes('"File"'))).toEqual([])
  })
})
