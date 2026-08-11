import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { nextTick } from 'vue'

const STORAGE_KEY = 'sidebar-group-prefs'

beforeEach(() => {
  vi.resetModules()
  localStorage.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe('useSidebar', () => {
  it('falls back to the default collapse state when no preference is stored', async () => {
    const { useSidebar } = await import('../../composables/useSidebar')
    const sidebar = useSidebar()

    expect(sidebar.isGroupCollapsed('runs', true)).toBe(true)
    expect(sidebar.isGroupCollapsed('runs', false)).toBe(false)
  })

  it('toggleGroup flips the collapse state from the default', async () => {
    const { useSidebar } = await import('../../composables/useSidebar')
    const sidebar = useSidebar()

    expect(sidebar.isGroupCollapsed('pipelines', true)).toBe(true)
    sidebar.toggleGroup('pipelines', true)
    expect(sidebar.isGroupCollapsed('pipelines', true)).toBe(false)
    sidebar.toggleGroup('pipelines', true)
    expect(sidebar.isGroupCollapsed('pipelines', true)).toBe(true)
  })

  it('a stored preference wins over the default', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ triggers: false }))
    const { useSidebar } = await import('../../composables/useSidebar')
    const sidebar = useSidebar()

    expect(sidebar.isGroupCollapsed('triggers', true)).toBe(false)
  })

  it('persists toggle state to localStorage', async () => {
    const { useSidebar } = await import('../../composables/useSidebar')
    const sidebar = useSidebar()

    sidebar.toggleGroup('agents', true)
    await nextTick()

    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}')
    expect(stored.agents).toBe(false)
  })

  it('is a module-level singleton shared across consumers', async () => {
    const { useSidebar } = await import('../../composables/useSidebar')
    const first = useSidebar()
    const second = useSidebar()

    first.toggleGroup('runs', true)
    expect(second.isGroupCollapsed('runs', true)).toBe(false)
  })

  it('restores preferences from a prior session on a fresh module import', async () => {
    const first = await import('../../composables/useSidebar')
    first.useSidebar().toggleGroup('runs', true)
    await nextTick()

    vi.resetModules()
    const second = await import('../../composables/useSidebar')
    expect(second.useSidebar().isGroupCollapsed('runs', true)).toBe(false)
  })

  it('exposes groupPrefs as a readonly ref', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const { useSidebar } = await import('../../composables/useSidebar')
    const sidebar = useSidebar()

    expect(sidebar.groupPrefs.value).toEqual({})

    // @ts-expect-error mutating a readonly ref
    sidebar.groupPrefs.value = { runs: true }

    expect(sidebar.groupPrefs.value).toEqual({})
    expect(warn).toHaveBeenCalled()
    warn.mockRestore()
  })
})
