import { test, expect, loginAsAdmin, isDevModeTarget, systemAdminJwt } from './setup/fixtures'

test.describe('Admin Node Categories', { tag: "@regression" }, () => {
  test('renders the Node Categories page', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(!isDevModeTarget(env), 'Route is dev-mode-gated (private_preview); only runs on a dev-mode target')
    await loginAsAdmin(page, env)
    if (env.name === 'local') {
      // The mock login token is not a JWT, so the manifest's *team role gate
      // (required_roles: [admin]) cannot be evaluated and sends the route to
      // the dashboard. Swap in a decodable JWT carrying org_role admin.
      await page.evaluate((token) => {
        localStorage.setItem('modulo_access_token', token)
      }, systemAdminJwt())
    }
    await page.goto('/admin/node-categories')
    await expect(page.locator('h1')).toContainText('Node Categories')
    if (env.name === 'local') {
      await expect(page.getByTestId('admin-node-categories-add')).toBeVisible()
    }
  })
})
