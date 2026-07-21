import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Feedback Inbox', () => {
  test('page loads with correct heading', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.goto('/feedback/inbox')
    await expect(page.locator('h1')).toContainText('Feedback Inbox')
    await expect(page.getByTestId('feedback-inbox-title')).toBeVisible()
  })
})
