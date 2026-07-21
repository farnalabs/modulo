import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Settings HITL Review', { tag: '@staging-regression' }, () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/hitl-review')
    await expect(page.locator('h1')).toContainText('HITL Review')
    await expect(page.getByTestId('hitl-review-status-select')).toBeVisible()
  })
})

test.describe('Settings Browser Monitoring', { tag: '@staging-regression' }, () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/monitoring')
    await expect(page.locator('h1')).toContainText('Browser Monitoring')
    await expect(page.locator('h1')).toBeVisible()
  })
})

test.describe('Settings Rate Limits', { tag: '@staging-regression' }, () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/rate-limits')
    await expect(page.locator('h1')).toContainText('Rate Limits')
    await expect(page.getByTestId('rate-limits-title')).toBeVisible()
  })
})

test.describe('Settings Remy Skills', { tag: '@staging-regression' }, () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/remy')
    await expect(page.locator('h1')).toContainText('Remy Skills')
    await expect(page.getByTestId('remy-user-skills-add')).toBeVisible()
  })
})

test.describe('Settings Runtime Config', { tag: '@staging-regression' }, () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/runtime-config')
    await expect(page.locator('h1')).toContainText('Runtime Config')
    await expect(page.getByTestId('settings-runtime-config-reload')).toBeVisible()
  })
})
