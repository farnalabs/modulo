import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import i18n, { detectBrowserLocale, isSupportedLocale, loadLocaleMessages, type SupportedLocale, SUPPORTED_LOCALES } from '../i18n'
import { api, getAccessToken } from '../lib/api/client'

const STORAGE_KEY = 'modulo_locale'

export const useLocaleStore = defineStore('locale', () => {
  const locale = ref<SupportedLocale>('en-US')
  const initialized = ref(false)

  function persist(code: SupportedLocale): void {
    try {
      localStorage.setItem(STORAGE_KEY, code)
    } catch {
      // localStorage may be unavailable
    }
  }

  async function setLocale(code: SupportedLocale): Promise<void> {
    if (!isSupportedLocale(code)) return
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
      })
    } catch {
      // Best-effort — don't block UI for backend sync failure
    }
  }

  async function initLocale(): Promise<void> {
    if (initialized.value) return
    initialized.value = true

    let detected: SupportedLocale = 'en-US'

    // 1. Try backend preferences
    if (getAccessToken() && !window.location.pathname.startsWith('/login')) {
      try {
        const res = await api.GET('/api/v1/me/settings')
        if (res.data) {
          const data = res.data as Record<string, unknown>
          const backendLocale = data?.locale as string | undefined
          if (backendLocale && isSupportedLocale(backendLocale)) {
            detected = backendLocale
          }
        }
      } catch {
        // Fall through
      }
    }

    // 2. Try localStorage
    if (detected === 'en-US') {
      try {
        const stored = localStorage.getItem(STORAGE_KEY)
        if (stored && isSupportedLocale(stored)) {
          detected = stored
        }
      } catch {
        // Fall through
      }
    }

    // 3. Try browser language
    if (detected === 'en-US') {
      detected = detectBrowserLocale()
    }

    try {
      await loadLocaleMessages(detected)
    } catch {
      console.warn(`[locale] Failed to load messages for ${detected}, falling back to en-US`)
      detected = 'en-US'
    }
    await setLocale(detected)
  }

  return {
    locale,
    initialized,
    SUPPORTED_LOCALES,
    setLocale,
    initLocale,
  }
})
