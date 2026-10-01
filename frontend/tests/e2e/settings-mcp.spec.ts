import { test, expect, loginAsAdmin, systemAdminJwt } from './setup/fixtures'
import type { Page } from '@playwright/test'
import type { TestEnv } from './setup/env'

/**
 * Sign in, then swap the opaque local mock token for a real admin JWT - the
 * API-keys card gates the "Create key" button on the `org_role` claim off the
 * access token, and the local mock-API login token carries none (same approach
 * as settings-mcp-oauth.spec.ts).
 */
async function loginForMcp(page: Page, env: TestEnv) {
  await loginAsAdmin(page, env)
  if (env.name === 'local') {
    await page.evaluate((token) => {
      localStorage.setItem('modulo_access_token', token)
    }, systemAdminJwt())
  }
}

/**
 * Intercept the Settings > MCP data surface. Must be registered AFTER login:
 * Playwright matches the most recently registered route first, so a route
 * registered before loginAsAdmin loses to the local mock-API catch-all.
 *
 * The view calls GET /api/v1/api-keys/mcp-config and GET /api/v1/api-keys
 * (plus the non-fatal GET /api/v1/mcp/oauth/clients) - never /api/v1/mcp/keys,
 * so the old glob matched nothing and this card's restricted/error state
 * depended on the live backend, flapping on a staging DB blip.
 */
async function mockSettingsMcpApi(page: Page) {
  // `**` (not `*`) so the `/api/v1/api-keys/mcp-config` sub-path is matched too:
  // a URL glob `*` does not cross a `/`.
  await page.route('**/api/v1/api-keys**', async (route) => {
    const url = route.request().url()
    if (url.includes('/mcp-config')) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ mcp_url: 'https://mcp.modulo.run' }),
      })
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        {
          id: 'mk1',
          name: 'Production Key',
          lookup_prefix: 'mod_abc',
          role: 'admin',
          created_at: '2025-06-01T10:00:00Z',
          expires_at: '2026-06-01T10:00:00Z',
          revoked_at: null,
          last_used_at: null,
        },
      ]),
    })
  })
  await page.route('**/api/v1/mcp/oauth/clients**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) }),
  )
}

test.describe('Settings MCP', { tag: "@regression" }, () => {
  test('page loads with correct heading', { tag: "@regression" }, async ({ page, env }) => {
    await loginForMcp(page, env)
    await mockSettingsMcpApi(page)

    await page.goto('/settings/mcp')

    await expect(page.locator('h1')).toContainText('MCP Configuration')
  })

  test('shows create key button and server URL section', { tag: "@regression" }, async ({ page, env }) => {
    await loginForMcp(page, env)
    await mockSettingsMcpApi(page)

    await page.goto('/settings/mcp')

    await expect(page.getByTestId('settings-mcp-create-key')).toBeVisible()
    await expect(page.getByTestId('settings-mcp-copy-url')).toBeVisible()
  })
})
