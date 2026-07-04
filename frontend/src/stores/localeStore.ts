import { defineStore } from 'pinia'
import { ref } from 'vue'
import i18n, { detectBrowserLocale, isSupportedLocale, loadLocaleMessages, type SupportedLocale, SUPPORTED_LOCALES } from '../i18n'
import { api, getAccessToken } from '../lib/api/client'

const STORAGE_KEY = 'modulo_locale'

export const useLocaleStore = defineStore('locale', () => {
  const locale = ref<SupportedLocale>('en-US')
  const initialized = ref(false)
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
      code = 'en-US'
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
      await api.PUT('/api/v1/me/settings', {
        locale: code,
      } as any)
    } catch (err) {
      console.warn('[locale] Failed to sync locale to backend', err)
    }
  }

  async function initLocale(): Promise<void> {
    if (initialized.value) return
    if (initPromise) return initPromise

    initPromise = (async () => {
      let detected: SupportedLocale = 'en-US'

      // 1. Try backend preferences (returns flat account.preferences dict)
      try {
        const res = await api.GET('/api/v1/me/settings')
        if (res.data) {
          const prefs = res.data as Record<string, unknown>
          const backendLocale = prefs?.locale as string | undefined
          if (backendLocale && isSupportedLocale(backendLocale)) {
            detected = backendLocale
          }
        }
      } catch (err) {
        console.warn('[locale] Failed to fetch locale from backend', err)
      }

      // 2. Try localStorage
      if (detected === 'en-US') {
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
      if (detected === 'en-US') {
        detected = detectBrowserLocale()
      }

      try {
        await loadLocaleMessages(detected)
      } catch (err) {
        console.warn(`[locale] Failed to load messages for ${detected}, falling back to en-US`, err)
        detected = 'en-US'
        await loadLocaleMessages(detected)
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
    SUPPORTED_LOCALES,
    setLocale,
    initLocale,
  }
})
