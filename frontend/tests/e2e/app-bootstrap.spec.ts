import { test, expect } from './setup/fixtures'

test.describe('App Bootstrap', () => {
  test('page loads without console errors', { tag: '@smoke' }, async ({ page }) => {
    const logs: any[] = []
    page.on('console', (msg) => {
      logs.push(msg)
    })

    await page.goto('/login', { timeout: 60000 })

    const consoleErrors = logs.filter(l => l.type() === 'error').map(l => l.text())
    const relevantErrors = consoleErrors.filter(e => !e.includes('MonitorBackendRegistry') && !e.includes('401'))
    expect(relevantErrors).toHaveLength(0)
  })

  test('login page displays key elements', async ({ page }) => {
    await page.goto('/login')

    await expect(page.locator('h1')).toContainText('Modulo')
    await expect(page.locator('text=Governed orchestration for your agentic SDLC')).toBeVisible()
    await expect(page.locator('button[type="submit"]')).toContainText('Sign in')
  })
})
