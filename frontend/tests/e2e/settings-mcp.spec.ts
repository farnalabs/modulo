import { test, expect } from '@playwright/test'

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.waitForLoadState('networkidle')
  await page.fill('input[type="text"]', 'admin@example.com')
  await page.fill('input[type="password"]', 'password123')
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 5000 }).catch(() => {})
}

test.describe('Settings MCP', () => {
  test('page loads with correct heading', async ({ page }) => {
    await page.route('**/api/v1/mcp/keys*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
    })
    await login(page)

    await page.goto('/settings/mcp')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText('MCP Configuration')
  })

  test('shows create key button and server URL section', async ({ page }) => {
    await page.route('**/api/v1/mcp/keys*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
    })
    await login(page)

    await page.goto('/settings/mcp')
    await page.waitForLoadState('networkidle')

    await expect(page.getByTestId('settings-mcp-create-key')).toBeVisible()
    await expect(page.getByTestId('settings-mcp-copy-url')).toBeVisible()
  })
})
