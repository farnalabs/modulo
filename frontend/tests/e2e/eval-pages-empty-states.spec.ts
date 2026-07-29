import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Eval Pages Empty States', () => {
  test('eval proposal, variant compare, and AB test pages show content (not blank) when empty', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    await page.goto('/evals/proposals')
    await expect(page.locator('#app')).not.toBeEmpty()

    await page.goto('/variants/compare')
    await expect(page.locator('#app')).not.toBeEmpty()

    await page.goto('/variants/ab-test')
    await expect(page.locator('#app')).not.toBeEmpty()
  })
})
