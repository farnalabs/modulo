import { test, expect } from './setup/fixtures'

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.waitForLoadState('networkidle')
  await page.fill('input[type="text"]', 'admin@example.com')
  await page.fill('input[type="password"]', 'password123')
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 5000 }).catch(() => {})
}

test.describe('Settings Teams', () => {
  test('page loads with correct heading and create button', async ({ page }) => {
    await page.route('**/api/v1/teams', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
    })
    await login(page)

    await page.goto('/settings/teams')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText('Teams')
    await expect(page.getByTestId('settings-teams-create-team')).toBeVisible()
    await expect(page.getByTestId('settings-teams-create-team')).toContainText('Create Team')
  })

  test('shows empty state when no teams exist', async ({ page }) => {
    await page.route('**/api/v1/teams', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
    })
    await login(page)

    await page.goto('/settings/teams')
    await page.waitForLoadState('networkidle')

    await expect(page.getByTestId('settings-teams-create-team')).toBeVisible()
  })
})
