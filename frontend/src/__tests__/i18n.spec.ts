import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import i18n, { loadLocaleMessages, isSupportedLocale, detectBrowserLocale, type SupportedLocale } from '../i18n'

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

describe('isSupportedLocale', () => {
  it('returns true for supported locale codes', () => {
    expect(isSupportedLocale('en-US')).toBe(true)
  })

  it('returns false for unsupported locale codes', () => {
    expect(isSupportedLocale('fr-FR')).toBe(false)
    expect(isSupportedLocale('de-DE')).toBe(false)
    expect(isSupportedLocale('')).toBe(false)
  })

  it('narrows the type correctly', () => {
    const code: string = 'en-US'
    if (isSupportedLocale(code)) {
      // TypeScript narrows to SupportedLocale
      const _narrowed: SupportedLocale = code
      expect(_narrowed).toBe('en-US')
    }
  })
})

describe('detectBrowserLocale', () => {
  const originalNavigator = global.navigator

  beforeEach(() => {
    vi.stubGlobal('navigator', { language: 'en-US' })
  })

  afterEach(() => {
    vi.stubGlobal('navigator', originalNavigator)
  })

  it('returns the exact match when navigator.language is supported', () => {
    vi.stubGlobal('navigator', { language: 'en-US' })
    expect(detectBrowserLocale()).toBe('en-US')
  })

  it('falls back to language prefix match when exact match fails', () => {
    vi.stubGlobal('navigator', { language: 'en-GB' })
    expect(detectBrowserLocale()).toBe('en-US')
  })

  it('falls back to DEFAULT_LOCALE when no prefix match exists', () => {
    vi.stubGlobal('navigator', { language: 'zh-CN' })
    expect(detectBrowserLocale()).toBe('en-US')
  })

  it('handles empty navigator.language gracefully', () => {
    vi.stubGlobal('navigator', { language: '' })
    expect(detectBrowserLocale()).toBe('en-US')
  })
})
