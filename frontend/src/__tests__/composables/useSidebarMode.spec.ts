import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useSidebarMode } from '../../composables/useSidebarMode'
import { usePlanStore } from '../../stores/planStore'
import { flagCacheKey, serializeFlagCache } from '../../config/flagCache'

vi.mock('../../lib/api/client', () => ({
  api: { GET: vi.fn().mockResolvedValue({ data: null, error: undefined }) },
  getAccessToken: vi.fn().mockReturnValue(null),
  clearAccessToken: vi.fn(),
  isDemoSession: vi.fn().mockReturnValue(false),
}))

function mockMatchMedia(desktop: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: desktop,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }))
}

describe('useSidebarMode (FAR-1237 — resolved-mode first paint)', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    mockMatchMedia(false) // mobile viewport unless a test overrides
  })

  afterEach(() => {
    delete (window as unknown as { matchMedia?: unknown }).matchMedia
  })

  it('is pending on mobile while no flag value has ever resolved', () => {
    const mode = useSidebarMode()
    expect(mode.mobileNavMode.value).toBe('pending')
    // The placeholder occupies the header slot, so the offset still applies.
    expect(mode.showMobileHeader.value).toBe(true)
  })

  it('resolves to the rail as soon as the flag is true', () => {
    usePlanStore().features['mobile_sidebar_rail'] = true
    const mode = useSidebarMode()
    expect(mode.mobileNavMode.value).toBe('rail')
    expect(mode.showMobileHeader.value).toBe(false)
  })

  it('resolves to the legacy drawer when a payload applied with the flag OFF (empty map = known, not unknown)', () => {
    usePlanStore().flagsSource = 'server'
    const mode = useSidebarMode()
    expect(mode.mobileNavMode.value).toBe('drawer')
    expect(mode.showMobileHeader.value).toBe(true)
  })

  it('a persisted cache resolves the mode synchronously — rail at first paint, before any fetch (primary UI)', () => {
    localStorage.setItem(flagCacheKey(null), serializeFlagCache({ mobile_sidebar_rail: true }))
    setActivePinia(createPinia()) // fresh store re-reads the cache

    const mode = useSidebarMode()

    expect(usePlanStore().loaded).toBe(false) // no request has resolved
    expect(mode.mobileNavMode.value).toBe('rail')
    expect(mode.showMobileHeader.value).toBe(false)
  })

  it('a persisted cache resolves the mode synchronously for the secondary UI too (drawer at first paint)', () => {
    localStorage.setItem(flagCacheKey(null), serializeFlagCache({ mobile_sidebar_rail: false }))
    setActivePinia(createPinia())

    const mode = useSidebarMode()

    expect(mode.mobileNavMode.value).toBe('drawer')
    expect(mode.showMobileHeader.value).toBe(true)
  })

  it('never falls back to pending once resolved — a failed refresh keeps the resolved mode', () => {
    const store = usePlanStore()
    store.flagsSource = 'cache'
    store.features = {} // refresh produced nothing new
    expect(useSidebarMode().mobileNavMode.value).toBe('drawer')
  })

  it('desktop never shows the mobile header chrome, pending or not', () => {
    mockMatchMedia(true)
    const mode = useSidebarMode()
    expect(mode.isDesktop.value).toBe(true)
    expect(mode.showMobileHeader.value).toBe(false)
  })

  it('desktop resolves no mobile header even when the rail flag is ON', () => {
    mockMatchMedia(true)
    usePlanStore().features['mobile_sidebar_rail'] = true
    const mode = useSidebarMode()
    expect(mode.isDesktop.value).toBe(true)
    expect(mode.mobileNavMode.value).toBe('rail')
    expect(mode.showMobileHeader.value).toBe(false)
  })
})
