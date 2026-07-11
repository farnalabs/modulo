import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('MonitorBackendRegistry typeof gating', () => {
  test('no TypeError from missing captureError/captureMessage on backends', { tag: '@e2e-regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    const consoleErrors: string[] = []
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        consoleErrors.push(msg.text())
      }
    })

    await page.goto('/')
    await page.waitForLoadState('networkidle')

    for (const err of consoleErrors) {
      expect(err).not.toContain('captureError is not a function')
      expect(err).not.toContain('captureMessage is not a function')
    }
  })
})
