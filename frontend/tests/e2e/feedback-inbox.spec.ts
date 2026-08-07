import { test, expect, loginAndGotoEnterprise } from './setup/fixtures'

test.describe('Feedback Inbox', { tag: "@regression" }, () => {
  test('page loads with correct heading', { tag: "@regression" }, async ({ page, env }) => {
    await loginAndGotoEnterprise(page, env, '/feedback/inbox')
    await expect(page.locator('h1')).toContainText('Feedback Inbox')
    await expect(page.getByTestId('feedback-inbox-title')).toBeVisible()
  })
})
