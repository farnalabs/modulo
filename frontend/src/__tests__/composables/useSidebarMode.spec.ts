import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useSidebarMode } from '../../composables/useSidebarMode'

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

describe('useSidebarMode', () => {
  beforeEach(() => mockMatchMedia(false))

  afterEach(() => {
    delete (window as unknown as { matchMedia?: unknown }).matchMedia
  })

  it('always selects the rail on mobile without consulting plan flags', () => {
    const mode = useSidebarMode()

    expect(mode.isDesktop.value).toBe(false)
    expect(mode.mobileNavMode).toBe('rail')
  })

  it('always selects the rail on desktop', () => {
    mockMatchMedia(true)
    const mode = useSidebarMode()

    expect(mode.isDesktop.value).toBe(true)
    expect(mode.mobileNavMode).toBe('rail')
  })
})
