import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useStorage } from '@vueuse/core'
import i18n, { detectBrowserLocale, isSupportedLocale, loadLocaleMessages, type SupportedLocale, SUPPORTED_LOCALES } from '../i18n'
import { api, getAccessToken } from '../lib/api/client'
import { withTimeout } from '../lib/asyncUtils'

const STORAGE_KEY = 'modulo_locale'
const DEFAULT_LOCALE: SupportedLocale = 'en-US'

export const useLocaleStore = defineStore('locale', () => {
  const locale = ref<SupportedLocale>('en-US')
  const initialized = ref(false)
  const error = ref<string | null>(null)
  const stored = useStorage<string>(STORAGE_KEY, DEFAULT_LOCALE)
  let initPromise: Promise<void> | null = null

  function persist(code: SupportedLocale): void {
    stored.value = code
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
        api.PUT('/api/v1/me/settings', { body: { locale: code } }),
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

      // 1. Try backend preferences — skip if not authenticated to avoid
      //    401 race on initial page load before auto-login completes.
      if (getAccessToken()) {
        try {
          const res = await withTimeout(
            api.GET('/api/v1/me/settings'),
            10000,
            'Locale fetch request',
          )
          const settingsData = res.data as { locale?: string } | undefined
          if (settingsData?.locale && isSupportedLocale(settingsData.locale)) {
            detected = settingsData.locale
          }
        } catch (err) {
          console.warn('[locale] Failed to fetch locale from backend', err)
        }
      }

      // 2. Try localStorage
      if (detected === DEFAULT_LOCALE) {
        const storedVal = stored.value
        if (storedVal && isSupportedLocale(storedVal)) {
          detected = storedVal
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
