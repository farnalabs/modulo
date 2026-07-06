import { defineStore } from 'pinia'
import { ref } from 'vue'
import i18n, { detectBrowserLocale, isSupportedLocale, loadLocaleMessages, type SupportedLocale, SUPPORTED_LOCALES } from '../i18n'
import { api, getAccessToken } from '../lib/api/client'
import { withTimeout } from '../lib/asyncUtils'

const STORAGE_KEY = 'modulo_locale'
const DEFAULT_LOCALE: SupportedLocale = 'en-US'

export const useLocaleStore = defineStore('locale', () => {
  const locale = ref<SupportedLocale>('en-US')
  const initialized = ref(false)
  const error = ref<string | null>(null)
  let initPromise: Promise<void> | null = null

  function persist(code: SupportedLocale): void {
    try {
      localStorage.setItem(STORAGE_KEY, code)
    } catch (err) {
      console.warn('[locale] Failed to persist locale to localStorage', err)
    }
  }

  async function setLocale(code: SupportedLocale): Promise<void> {
    if (!isSupportedLocale(code)) return
    try {
      await loadLocaleMessages(code)
    } catch (err) {
      console.warn(`[locale] Failed to load messages for ${code}, falling back to en-US`, err)
      code = DEFAULT_LOCALE
      try {
        await loadLocaleMessages(code)
      } catch {
        error.value = 'Failed to load locale messages'
        return
      }
    }
    locale.value = code
    i18n.global.locale.value = code
    persist(code)
    document.documentElement.lang = code
    await syncToBackend(code)
  }

  async function syncToBackend(code: SupportedLocale): Promise<void> {
    if (!getAccessToken()) return
    try {
      await withTimeout(
        api.PUT('/api/v1/me/settings', { locale: code } as any),
        10000,
        'Locale sync request',
      )
    } catch (err) {
      console.warn('[locale] Failed to sync locale to backend', err)
    }
  }

  async function initLocale(): Promise<void> {
    if (initialized.value) return
    if (initPromise) return initPromise

    initPromise = (async () => {
      let detected: SupportedLocale = DEFAULT_LOCALE

      // 1. Try backend preferences (returns flat account.preferences dict)
      try {
        const res = await withTimeout(
          api.GET('/api/v1/me/settings'),
          10000,
          'Locale fetch request',
        )
        if (res.data) {
          const backendLocale = (res.data as Record<string, unknown>).locale as string | undefined
          if (backendLocale && isSupportedLocale(backendLocale)) {
            detected = backendLocale
          }
        }
      } catch (err) {
        console.warn('[locale] Failed to fetch locale from backend', err)
      }

      // 2. Try localStorage
      if (detected === DEFAULT_LOCALE) {
        try {
          const stored = localStorage.getItem(STORAGE_KEY)
          if (stored && isSupportedLocale(stored)) {
            detected = stored
          }
        } catch (err) {
          console.warn('[locale] Failed to read locale from localStorage', err)
        }
      }

      // 3. Try browser language
      if (detected === DEFAULT_LOCALE) {
        detected = detectBrowserLocale()
      }

      try {
        await loadLocaleMessages(detected)
      } catch (err) {
        console.warn(`[locale] Failed to load messages for ${detected}, falling back to en-US`, err)
        detected = DEFAULT_LOCALE
        try {
          await loadLocaleMessages(detected)
        } catch (fallbackErr) {
          error.value = 'Failed to load locale messages'
          console.warn('[locale] Failed to load even fallback locale', fallbackErr)
          initPromise = null
          return
        }
      }
      await setLocale(detected)
      initialized.value = true
      initPromise = null
    })()

    return initPromise
  }

  return {
    locale,
    initialized,
    error,
    SUPPORTED_LOCALES,
    setLocale,
    initLocale,
  }
})
