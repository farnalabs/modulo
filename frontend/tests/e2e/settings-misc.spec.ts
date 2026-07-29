import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Settings HITL Review', { tag: "@regression" }, () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/hitl-review')
    await expect(page.locator('h1')).toBeVisible()
    await expect(page.getByTestId('hitl-review-status-select')).toBeVisible()
  })
})

test.describe('Settings Browser Monitoring', { tag: "@regression" }, () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/monitoring')
    await expect(page.locator('h1')).toBeVisible()
    await expect(page.locator('h1')).toBeVisible()
  })
})

test.describe('Settings Rate Limits', { tag: "@regression" }, () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/rate-limits')
    await expect(page.locator('h1')).toBeVisible()
    await expect(page.getByTestId('rate-limits-title')).toBeVisible()
  })
})

test.describe('Settings Remy Skills', { tag: "@regression" }, () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/remy')
    await expect(page.locator('h1')).toBeVisible()
    await expect(page.getByTestId('remy-user-skills-add')).toBeVisible()
  })
})

test.describe('Settings Runtime Config', { tag: "@regression" }, () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/settings/runtime-config')
    await expect(page.locator('h1')).toBeVisible()
    await expect(page.getByTestId('settings-runtime-config-reload')).toBeVisible()
  })
})
