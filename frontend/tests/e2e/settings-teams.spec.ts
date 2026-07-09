import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Settings Teams', () => {
  test('page loads with correct heading and create button', async ({ page, env }) => {
    await page.route('**/api/v1/teams', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 't1', name: 'Engineering', description: 'Core engineering team', member_count: 5, created_at: '2025-01-15T10:00:00Z' }], total: 1 }) })
    })
    await loginAsAdmin(page, env)

    await page.goto('/settings/teams')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText('Teams')
    await expect(page.getByTestId('settings-teams-create-team')).toBeVisible()
    await expect(page.getByTestId('settings-teams-create-team')).toContainText('Create Team')
  })

  test('shows empty state when no teams exist', async ({ page, env }) => {
    await page.route('**/api/v1/teams', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 't1', name: 'Engineering', description: 'Core engineering team', member_count: 5, created_at: '2025-01-15T10:00:00Z' }], total: 1 }) })
    })
    await loginAsAdmin(page, env)

    await page.goto('/settings/teams')
    await page.waitForLoadState('networkidle')

    await expect(page.getByTestId('settings-teams-create-team')).toBeVisible()
  })
})
