import { test, expect, loginAsAdmin, isDevModeTarget } from './setup/fixtures'

test.describe('Settings HITL Review', { tag: "@regression" }, () => {
  test('renders the HITL Review page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/hitl-review')
    await expect(page.locator('h1')).toContainText('HITL Review')
    if (env.name === 'local') {
      await expect(page.getByTestId('hitl-review-status-select')).toBeVisible()
    }
  })
})

test.describe('Settings Browser Monitoring', { tag: "@regression" }, () => {
  test('renders the Browser Monitoring page', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(!isDevModeTarget(env), 'Route is dev-mode-gated (private_preview); only runs on a dev-mode target')
    await loginAsAdmin(page, env)
    await page.goto('/settings/monitoring')
    await expect(page.locator('h1')).toContainText('Browser Monitoring')
  })
})

test.describe('Settings Rate Limits', { tag: "@regression" }, () => {
  test('renders the Rate Limits page', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(!isDevModeTarget(env), 'Route is dev-mode-gated (private_preview); only runs on a dev-mode target')
    await loginAsAdmin(page, env)
    await page.goto('/settings/rate-limits')
    await expect(page.locator('h1')).toContainText('Rate Limits')
    if (env.name === 'local') {
      await expect(page.getByTestId('rate-limits-title')).toBeVisible()
    }
  })
})

test.describe('Settings Remy Skills', { tag: "@regression" }, () => {
  test('renders the Remy Skills page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/remy')
    await expect(page.locator('h1')).toContainText('My Remy Skills')
    if (env.name === 'local') {
      await expect(page.getByTestId('remy-user-skills-add')).toBeVisible()
    }
  })
})

test.describe('Settings Runtime Config', { tag: "@regression" }, () => {
  test('renders the Runtime Config page', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(!isDevModeTarget(env), 'Route is dev-mode-gated (private_preview); only runs on a dev-mode target')
    await loginAsAdmin(page, env)
    await page.goto('/settings/runtime-config')
    await expect(page.locator('h1')).toContainText('Runtime Configuration')
    if (env.name === 'local') {
      await expect(page.getByTestId('settings-runtime-config-reload')).toBeVisible()
    }
  })
})
