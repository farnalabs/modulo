import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Remy i18n Keys Regression', () => {
  test('no missing remy i18n key warnings', { tag: '@e2e-regression' }, async ({ page, env }) => {
    const intlifyWarnings: string[] = []
    page.on('console', (msg) => {
      if (msg.type() === 'warn' && msg.text().includes("[intlify] Not found 'remy.")) {
        intlifyWarnings.push(msg.text())
      }
    })

    await loginAsAdmin(page, env)
    await page.goto('/dashboard')
    await page.waitForLoadState('networkidle')

    expect(intlifyWarnings).toEqual([])
  })
})
