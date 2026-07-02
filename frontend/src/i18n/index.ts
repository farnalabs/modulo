import { createI18n } from 'vue-i18n'

const DEFAULT_LOCALE = 'en-US'

export const SUPPORTED_LOCALES = [
  { code: 'en-US', label: 'English (US)', flag: '🇺🇸' },
] as const

export type SupportedLocale = (typeof SUPPORTED_LOCALES)[number]['code']

export function isSupportedLocale(code: string): code is SupportedLocale {
  return SUPPORTED_LOCALES.some((l) => l.code === code)
}

export function detectBrowserLocale(): SupportedLocale {
  const raw = navigator.language
  if (isSupportedLocale(raw)) return raw
  const lang = raw.split('-')[0]
  const match = SUPPORTED_LOCALES.find((l) => l.code.startsWith(lang))
  return match?.code ?? DEFAULT_LOCALE
}

const i18n = createI18n({
  legacy: false,
  locale: DEFAULT_LOCALE,
  fallbackLocale: DEFAULT_LOCALE,
  messages: {},
  datetimeFormats: {
    'en-US': {
      short: { year: 'numeric', month: 'short', day: 'numeric' },
      medium: { year: 'numeric', month: 'long', day: 'numeric' },
      long: { year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit' },
    },
  },
  numberFormats: {
    'en-US': {
      decimal: { style: 'decimal', minimumFractionDigits: 0, maximumFractionDigits: 2 },
      percent: { style: 'percent', minimumFractionDigits: 0, maximumFractionDigits: 1 },
      currency: { style: 'currency', currency: 'USD' },
    },
  },
})

export async function loadLocaleMessages(locale: SupportedLocale): Promise<void> {
  if (i18n.global.availableLocales.includes(locale)) return
  try {
    const messages = await import(`../locales/${locale}.json`)
    i18n.global.setLocaleMessage(locale, messages.default ?? messages)
  } catch (e) {
    console.warn(`Failed to load locale messages for "${locale}", falling back to en-US`, e)
    if (locale !== 'en-US') {
      await import(`../locales/en-US.json`)
        .then((m) => i18n.global.setLocaleMessage('en-US', m.default ?? m))
    }
  }
}

export default i18n
