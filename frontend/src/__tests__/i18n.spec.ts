import { describe, expect, it } from 'vitest'
import i18n, { loadLocaleMessages, type SupportedLocale } from '../i18n'

describe('loadLocaleMessages', () => {
  it('resolves without re-registering a locale whose messages are already loaded', async () => {
    expect(i18n.global.availableLocales).toContain('en-US')

    await expect(loadLocaleMessages('en-US')).resolves.toBeUndefined()
    expect(i18n.global.availableLocales).toContain('en-US')
  })

  it('registers the bundled messages for a locale that is not loaded yet', async () => {
    // Only en-US ships today; the cast exercises the registration branch a
    // future locale will take.
    const locale = 'fr-FR' as SupportedLocale
    expect(i18n.global.availableLocales).not.toContain(locale)

    await expect(loadLocaleMessages(locale)).resolves.toBeUndefined()

    expect(i18n.global.availableLocales).toContain(locale)
  })
})
