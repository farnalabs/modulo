import { test, expect, loginAsAdmin, systemAdminJwt } from './setup/fixtures'
import type { Page } from '@playwright/test'
import type { TestEnv } from './setup/env'

interface RegisteredClient {
  id: string
  client_id: string
  name: string
  scopes: string[]
  redirect_uris: string[]
  created_at: string
}

const SEED_CLIENT: RegisteredClient = {
  id: 'client-e2e-1',
  client_id: 'mod_oauth_e2e_client',
  name: 'E2E OAuth Client',
  scopes: ['trigger:run'],
  redirect_uris: ['https://example.com/callback'],
  created_at: '2026-06-20T00:00:00Z',
}

/**
 * Sign in, then swap the opaque local mock token for a real admin JWT - the
 * OAuth client section reads the org_role claim off the access token, and the
 * local mock-API login token carries none (same approach as
 * loginForSystemRoute / assistant-only).
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
 */
async function mockSettingsMcpApi(page: Page) {
  await page.route('**/api/v1/api-keys*', async (route) => {
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
      body: JSON.stringify([]),
    })
  })
}

/** The OAuth client list, with stateful POST / DELETE handling. */
async function mockOAuthClientsApi(page: Page, existing: RegisteredClient[]) {
  const clients = [...existing]

  await page.route('**/api/v1/mcp/oauth/clients*', async (route) => {
    const request = route.request()
    if (request.method() === 'POST') {
      clients.push(SEED_CLIENT)
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          id: SEED_CLIENT.id,
          client_id: SEED_CLIENT.client_id,
          client_secret: 'mod_oauth_e2e_secret_value',
          name: SEED_CLIENT.name,
        }),
      })
    }
    if (request.method() === 'DELETE') {
      clients.splice(0, clients.length)
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ deleted: true }),
      })
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(clients),
    })
  })
}

async function gotoMcpSettings(page: Page) {
  await page.goto('/settings/mcp')
  await page
    .waitForSelector('[data-loading="false"]', { timeout: 15000 })
    .catch(() => {})
}

test.describe('Settings MCP OAuth clients', { tag: '@regression' }, () => {
  test('registers an OAuth client and reveals the credentials once', { tag: '@regression' }, async ({ page, env }) => {
    await loginForMcp(page, env)
    await mockSettingsMcpApi(page)
    await mockOAuthClientsApi(page, [])
    await gotoMcpSettings(page)

    await expect(page.getByTestId('settings-mcp-register-oauth-client')).toBeVisible()
    await expect(page.getByTestId('settings-mcp-oauth-empty')).toBeVisible()

    await page.getByTestId('settings-mcp-register-oauth-client').click()
    await expect(page.getByTestId('settings-mcp-oauth-name')).toBeVisible()

    // Inline validation on blur
    await page.getByTestId('settings-mcp-oauth-name').blur()
    await expect(page.getByTestId('settings-mcp-oauth-name-error')).toBeVisible()

    await page.getByTestId('settings-mcp-oauth-name').fill('E2E OAuth Client')
    await page.getByTestId('settings-mcp-oauth-redirect-uris').fill('https://example.com/callback')
    await page.getByTestId('settings-mcp-oauth-scope-trigger-run').check()

    const registerDialog = page
      .locator('[role="dialog"]')
      .filter({ hasText: 'Register an OAuth client application' })
    await registerDialog.getByRole('button', { name: 'Register OAuth Client' }).click()

    // One-time reveal dialog: client id + client secret with copy buttons
    const createdDialog = page.locator('[role="dialog"]').filter({ hasText: 'Copy these credentials now' })
    await expect(createdDialog).toBeVisible()
    await expect(createdDialog.getByTestId('settings-mcp-oauth-client-id')).toHaveValue('mod_oauth_e2e_client')
    await expect(createdDialog.getByTestId('settings-mcp-oauth-client-secret')).toBeVisible()
    await expect(createdDialog.getByTestId('settings-mcp-copy-oauth-client-id')).toBeVisible()
    await expect(createdDialog.getByTestId('settings-mcp-copy-oauth-client-secret')).toBeVisible()
    await createdDialog.getByTestId('settings-mcp-oauth-created-done').click()

    // The list is refreshed with the newly registered client
    await expect(page.getByTestId('settings-mcp-oauth-empty')).toHaveCount(0)
    await expect(page.getByText('mod_oauth_e2e_client').first()).toBeVisible()
  })

  test('revokes an OAuth client after confirmation', { tag: '@regression' }, async ({ page, env }) => {
    await loginForMcp(page, env)
    await mockSettingsMcpApi(page)
    await mockOAuthClientsApi(page, [SEED_CLIENT])
    await gotoMcpSettings(page)

    await expect(page.getByText('mod_oauth_e2e_client').first()).toBeVisible()
    await page.getByTestId('settings-mcp-revoke-oauth-client').first().click()

    const confirmDialog = page
      .locator('[role="dialog"]')
      .filter({ hasText: 'Are you sure you want to revoke the OAuth client' })
    await expect(confirmDialog).toBeVisible()
    await expect(confirmDialog).toContainText('E2E OAuth Client')
    await confirmDialog.getByRole('button', { name: 'Confirm Revoke' }).click()

    await expect(page.getByTestId('settings-mcp-oauth-empty')).toBeVisible()
  })
})
