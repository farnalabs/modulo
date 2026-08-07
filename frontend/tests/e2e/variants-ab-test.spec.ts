import { test, expect, loginAndGotoEnterprise } from './setup/fixtures'

test.describe('AB Test Models', { tag: "@regression" }, () => {
  test('page loads with correct heading', { tag: "@regression" }, async ({ page, env }) => {
    await loginAndGotoEnterprise(page, env, '/variants/ab-test')
    await expect(page.locator('h1')).toContainText('A/B Test Models')
    await expect(page.getByTestId('ab-test-models-pipeline-select')).toBeVisible()
  })
})
