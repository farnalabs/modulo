import { test, expect, loginAsAdmin, isDevModeTarget } from './setup/fixtures'

test.describe('Settings Email', { tag: "@regression" }, () => {
  test('renders the Email Settings page', { tag: "@regression" }, async ({ page, env }) => {
    if (env.name === 'local') {
      await page.route('**/api/v1/email/config*', (route) => {
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ smtp_host: 'smtp.example.com', smtp_port: 587, from_address: 'noreply@example.com', encryption: 'tls', enabled: true }) })
      })
    }
    await loginAsAdmin(page, env)
    await page.goto('/settings/email')
    await expect(page.locator('h1')).toContainText('Email Settings')
  })
})

test.describe('Settings Error Forwarders', { tag: "@regression" }, () => {
  test('renders the Error Forwarders page', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(!isDevModeTarget(env), 'Route is dev-mode-gated (private_preview); only runs on a dev-mode target')
    await loginAsAdmin(page, env)
    await page.goto('/settings/error-forwarders')
    await expect(page.locator('h1')).toContainText('Error Forwarders')
  })
})

test.describe('Settings Observability', { tag: "@regression" }, () => {
  test('renders the Observability page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/observability')
    await expect(page.locator('h1')).toContainText('Observability')
    if (env.name === 'local') {
      await expect(page.getByTestId('settings-observability-otlp-endpoint')).toBeVisible()
    }
  })
})
