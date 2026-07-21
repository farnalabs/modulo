import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Settings Email', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await page.route('**/api/v1/email/config*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ smtp_host: 'smtp.example.com', smtp_port: 587, from_address: 'noreply@example.com', encryption: 'tls', enabled: true }) })
    })
    await loginAsAdmin(page, env)
    await page.goto('/settings/email')
    await expect(page.locator('h1')).toContainText('Email Settings')
    await expect(page.locator('h1')).toBeVisible()
  })
})

test.describe('Settings Error Forwarders', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/error-forwarders')
    await expect(page.locator('h1')).toContainText('Error Forwarders')
    await expect(page.locator('h1')).toBeVisible()
  })
})

test.describe('Settings Observability', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/observability')
    await expect(page.locator('h1')).toContainText('Observability')
    await expect(page.getByTestId('settings-observability-otlp-endpoint')).toBeVisible()
  })
})
