import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Settings MCP', { tag: "@regression" }, () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await page.route('**/api/v1/mcp/keys*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'mk1', name: 'Production Key', prefix: 'mod_abc', created_at: '2025-06-01T10:00:00Z', expires_at: '2026-06-01T10:00:00Z' }], total: 1 }) })
    })
    await loginAsAdmin(page, env)

    await page.goto('/settings/mcp')

    await expect(page.locator('h1')).toContainText('MCP Configuration')
  })

  test('shows create key button and server URL section', async ({ page, env }) => {
    await page.route('**/api/v1/mcp/keys*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'mk1', name: 'Production Key', prefix: 'mod_abc', created_at: '2025-06-01T10:00:00Z', expires_at: '2026-06-01T10:00:00Z' }], total: 1 }) })
    })
    await loginAsAdmin(page, env)

    await page.goto('/settings/mcp')

    await expect(page.getByTestId('settings-mcp-create-key')).toBeVisible()
    await expect(page.getByTestId('settings-mcp-copy-url')).toBeVisible()
  })
})
