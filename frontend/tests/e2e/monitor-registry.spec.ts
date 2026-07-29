import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('MonitorBackendRegistry typeof gating', { tag: '@regression' }, () => {
  test('no TypeError from missing captureError/captureMessage on backends', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    const consoleErrors: string[] = []
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        consoleErrors.push(msg.text())
      }
    })

    await page.goto('/')

    for (const err of consoleErrors) {
      expect(err).not.toContain('captureError is not a function')
      expect(err).not.toContain('captureMessage is not a function')
    }
  })
})
