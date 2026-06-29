import { test, expect } from '@playwright/test'

test.describe('App Bootstrap', () => {
  test('page loads without console errors', async ({ page }) => {
    const consoleErrors: string[] = []
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        consoleErrors.push(msg.text())
      }
    })

    await page.goto('/login')
    await page.waitForLoadState('networkidle')

    expect(consoleErrors).toHaveLength(0)
  })

  test('login page displays key elements', async ({ page }) => {
    await page.goto('/login')
    await page.waitForLoadState('networkidle')

    await expect(page.locator('h1')).toContainText('Modulo')
    await expect(page.locator('text=SDLC pipeline orchestration')).toBeVisible()
    await expect(page.locator('button[type="submit"]')).toContainText('Sign in')
  })
})
