import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Settings HITL Review', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/hitl-review')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('HITL Review')
    await expect(page.getByTestId('hitl-review-status-select')).toBeVisible()
  })
})

test.describe('Settings Browser Monitoring', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/monitoring')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Browser Monitoring')
    await expect(page.locator('h1')).toBeVisible()
  })
})

test.describe('Settings Rate Limits', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/rate-limits')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Rate Limits')
    await expect(page.getByTestId('rate-limits-title')).toBeVisible()
  })
})

test.describe('Settings Remy Skills', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/remy')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Remy Skills')
    await expect(page.getByTestId('remy-user-skills-add')).toBeVisible()
  })
})

test.describe('Settings Runtime Config', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/runtime-config')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('h1')).toContainText('Runtime Config')
    await expect(page.getByTestId('settings-runtime-config-reload')).toBeVisible()
  })
})
