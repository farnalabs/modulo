import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const apiGet = vi.hoisted(() => vi.fn())
const apiPut = vi.hoisted(() => vi.fn())
const getAccessTokenMock = vi.hoisted(() => vi.fn())
const isSupportedLocaleMock = vi.hoisted(() => vi.fn())
const loadLocaleMessagesMock = vi.hoisted(() => vi.fn())
const detectBrowserLocaleMock = vi.hoisted(() => vi.fn())
const i18nGlobalLocale = vi.hoisted(() => ({ value: 'en-US' }))

vi.mock('../../lib/api/client', () => ({
  api: { GET: apiGet, PUT: apiPut },
  getAccessToken: getAccessTokenMock,
}))

vi.mock('../../i18n', () => ({
  default: { global: { locale: i18nGlobalLocale } },
  isSupportedLocale: isSupportedLocaleMock,
  loadLocaleMessages: loadLocaleMessagesMock,
  detectBrowserLocale: detectBrowserLocaleMock,
  SUPPORTED_LOCALES: [
    { code: 'en-US', label: 'English (US)' },
    { code: 'fr-FR', label: 'Français' },
  ],
}))

import { setActivePinia, createPinia } from 'pinia'
import { useLocaleStore } from '../../stores/localeStore'

const STORAGE_KEY = 'modulo_locale'
const SUPPORTED = ['en-US', 'fr-FR']

let warnSpy: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  document.documentElement.lang = ''
  warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
  isSupportedLocaleMock.mockImplementation((code: string) => SUPPORTED.includes(code))
  loadLocaleMessagesMock.mockResolvedValue(undefined)
  detectBrowserLocaleMock.mockReturnValue('en-US')
  getAccessTokenMock.mockReturnValue(null)
  apiGet.mockReset()
  apiPut.mockReset()
  apiPut.mockResolvedValue({ data: null, error: null })
})

afterEach(() => {
  warnSpy.mockRestore()
  vi.clearAllMocks()
})

describe('useLocaleStore', () => {
  it('starts with the default locale and uninitialized state', () => {
    const store = useLocaleStore()
    expect(store.locale).toBe('en-US')
    expect(store.initialized).toBe(false)
    expect(store.error).toBeNull()
    expect(store.SUPPORTED_LOCALES).toHaveLength(2)
  })

  it('setLocale is a no-op for an unsupported locale', async () => {
    const store = useLocaleStore()

    await store.setLocale('de-DE' as never)

    expect(loadLocaleMessagesMock).not.toHaveBeenCalled()
    expect(store.locale).toBe('en-US')
    expect(localStorage.getItem(STORAGE_KEY)).toBe('en-US')
    expect(apiPut).not.toHaveBeenCalled()
  })

  it('setLocale updates locale, i18n, persistence and document lang', async () => {
    getAccessTokenMock.mockReturnValue('token-1')
    const store = useLocaleStore()

    await store.setLocale('fr-FR' as never)

    expect(loadLocaleMessagesMock).toHaveBeenCalledWith('fr-FR')
    expect(store.locale).toBe('fr-FR')
    expect(i18nGlobalLocale.value).toBe('fr-FR')
    expect(localStorage.getItem(STORAGE_KEY)).toBe('fr-FR')
    expect(document.documentElement.lang).toBe('fr-FR')
  })

  it('setLocale persists to the backend when authenticated', async () => {
    getAccessTokenMock.mockReturnValue('token-1')
    const store = useLocaleStore()

    await store.setLocale('fr-FR' as never)

    expect(apiPut).toHaveBeenCalledWith('/api/v1/me/settings', { body: { locale: 'fr-FR' } })
  })

  it('setLocale skips the backend sync when unauthenticated', async () => {
    getAccessTokenMock.mockReturnValue(null)
    const store = useLocaleStore()

    await store.setLocale('fr-FR' as never)

    expect(apiPut).not.toHaveBeenCalled()
    expect(store.locale).toBe('fr-FR')
  })

  it('setLocale falls back to en-US when loading the requested messages fails', async () => {
    loadLocaleMessagesMock.mockImplementation((code: string) =>
      code === 'fr-FR' ? Promise.reject(new Error('missing fr')) : Promise.resolve(undefined),
    )
    const store = useLocaleStore()

    await store.setLocale('fr-FR' as never)

    expect(warnSpy).toHaveBeenCalled()
    expect(store.locale).toBe('en-US')
    expect(localStorage.getItem(STORAGE_KEY)).toBe('en-US')
  })

  it('setLocale records an error when even the fallback messages fail', async () => {
    loadLocaleMessagesMock.mockRejectedValue(new Error('i18n down'))
    const store = useLocaleStore()

    await store.setLocale('fr-FR' as never)

    expect(store.error).toBe('Failed to load locale messages')
    expect(store.locale).toBe('en-US')
    expect(apiPut).not.toHaveBeenCalled()
  })

  it('syncToBackend swallows a rejected request without throwing', async () => {
    getAccessTokenMock.mockReturnValue('token-1')
    apiPut.mockRejectedValue(new Error('network'))
    const store = useLocaleStore()

    await expect(store.setLocale('fr-FR' as never)).resolves.toBeUndefined()

    expect(warnSpy).toHaveBeenCalled()
    expect(store.locale).toBe('fr-FR')
  })

  it('initLocale prefers the backend locale preference', async () => {
    getAccessTokenMock.mockReturnValue('token-1')
    apiGet.mockResolvedValue({ data: { locale: 'fr-FR' }, error: null })
    const store = useLocaleStore()

    await store.initLocale()

    expect(apiGet).toHaveBeenCalledWith('/api/v1/me/settings')
    expect(store.locale).toBe('fr-FR')
    expect(store.initialized).toBe(true)
  })

  it('initLocale ignores an unsupported backend locale', async () => {
    getAccessTokenMock.mockReturnValue('token-1')
    apiGet.mockResolvedValue({ data: { locale: 'xx-XX' }, error: null })
    const store = useLocaleStore()

    await store.initLocale()

    expect(store.locale).toBe('en-US')
    expect(detectBrowserLocaleMock).toHaveBeenCalled()
  })

  it('initLocale tolerates a failing backend fetch and falls through', async () => {
    getAccessTokenMock.mockReturnValue('token-1')
    apiGet.mockRejectedValue(new Error('backend down'))
    const store = useLocaleStore()

    await expect(store.initLocale()).resolves.toBeUndefined()

    expect(warnSpy).toHaveBeenCalled()
    expect(store.locale).toBe('en-US')
    expect(store.initialized).toBe(true)
  })

  it('initLocale prefers the stored preference over browser detection', async () => {
    localStorage.setItem(STORAGE_KEY, 'fr-FR')
    const store = useLocaleStore()

    await store.initLocale()

    expect(detectBrowserLocaleMock).not.toHaveBeenCalled()
    expect(store.locale).toBe('fr-FR')
    expect(store.initialized).toBe(true)
  })

  it('initLocale falls back to the browser locale when nothing else is set', async () => {
    detectBrowserLocaleMock.mockReturnValue('fr-FR')
    const store = useLocaleStore()

    await store.initLocale()

    expect(detectBrowserLocaleMock).toHaveBeenCalled()
    expect(store.locale).toBe('fr-FR')
    expect(store.initialized).toBe(true)
  })

  it('initLocale records an error when no locale messages can be loaded', async () => {
    loadLocaleMessagesMock.mockRejectedValue(new Error('i18n down'))
    detectBrowserLocaleMock.mockReturnValue('fr-FR')
    const store = useLocaleStore()

    await store.initLocale()

    expect(store.error).toBe('Failed to load locale messages')
    expect(store.initialized).toBe(false)
  })

  it('initLocale dedupes concurrent invocations onto a single fetch', async () => {
    getAccessTokenMock.mockReturnValue('token-1')
    apiGet.mockResolvedValue({ data: { locale: 'fr-FR' }, error: null })
    const store = useLocaleStore()

    const results = await Promise.all([store.initLocale(), store.initLocale()])

    expect(apiGet).toHaveBeenCalledTimes(1)
    expect(results).toEqual([undefined, undefined])
    expect(store.locale).toBe('fr-FR')
  })

  it('initLocale is a no-op once initialized', async () => {
    const store = useLocaleStore()

    await store.initLocale()
    expect(store.initialized).toBe(true)

    expect(await store.initLocale()).toBeUndefined()
  })
})
