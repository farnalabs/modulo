import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Evals', () => {
  test('eval editor page loads', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/evals/editor')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText(/Eval|Editor/i)
  })

  test('eval proposals page loads', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/evals/proposals')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText(/Proposal/i)
  })

  test('variants compare page loads', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/variants/compare')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText(/Variant|Compare/i)
  })
})
