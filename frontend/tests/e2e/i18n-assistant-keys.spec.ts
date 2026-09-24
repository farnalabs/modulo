import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Assistant i18n Keys Regression', () => {
  test('no missing assistant i18n key warnings', { tag: "@regression" }, async ({ page, env }) => {
    const intlifyWarnings: string[] = []
    page.on('console', (msg) => {
      if (msg.type() === 'warn' && msg.text().includes("[intlify] Not found 'assistant.")) {
        intlifyWarnings.push(msg.text())
      }
    })

    await loginAsAdmin(page, env)
    await page.goto('/dashboard')

    expect(intlifyWarnings).toEqual([])
  })
})
