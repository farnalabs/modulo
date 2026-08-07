import { test, expect, loginAndGotoEnterprise, spaNavigate } from './setup/fixtures'

test.describe('Eval Pages Empty States', () => {
  test('eval proposal, variant compare, and AB test pages show content (not blank) when empty', { tag: "@regression" }, async ({ page, env }) => {
    await loginAndGotoEnterprise(page, env, '/evals/proposals')
    await expect(page.locator('#app')).not.toBeEmpty()

    await spaNavigate(page, '/variants/compare')
    await expect(page.locator('#app')).not.toBeEmpty()

    await spaNavigate(page, '/variants/ab-test')
    await expect(page.locator('#app')).not.toBeEmpty()
  })
})
