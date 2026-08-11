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

async function setupSidebar() {
  const mod = await import('../../composables/useSidebar')
  return mod.useSidebar()
}

describe('useSidebar', () => {
  it('falls back to the default collapse state when no preference is stored', async () => {
    const sidebar = await setupSidebar()

    expect(sidebar.isGroupCollapsed('runs', true)).toBe(true)
    expect(sidebar.isGroupCollapsed('runs', false)).toBe(false)
  })

  it('toggleGroup flips a collapsed default to expanded', async () => {
    const sidebar = await setupSidebar()
    expect(sidebar.isGroupCollapsed('runs', true)).toBe(true)
    sidebar.toggleGroup('runs', true)
    expect(sidebar.isGroupCollapsed('runs', true)).toBe(false)
  })

  it('toggleGroup flips an expanded default to collapsed', async () => {
    const sidebar = await setupSidebar()
    expect(sidebar.isGroupCollapsed('agents', false)).toBe(false)
    sidebar.toggleGroup('agents', false)
    expect(sidebar.isGroupCollapsed('agents', false)).toBe(true)
  })

  it('toggles back and forth across repeated calls', async () => {
    const sidebar = await setupSidebar()
    sidebar.toggleGroup('runs', true)
    sidebar.toggleGroup('runs', true)
    expect(sidebar.isGroupCollapsed('runs', true)).toBe(true)
    sidebar.toggleGroup('runs', true)
    expect(sidebar.isGroupCollapsed('runs', true)).toBe(false)
  })

  it('persists toggled prefs to localStorage', async () => {
    const sidebar = await setupSidebar()
    sidebar.toggleGroup('runs', true)
    await vi.waitFor(() => expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}')).toEqual({ runs: false }))
  })

  it('reads a persisted pref back through isGroupCollapsed', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ runs: false }))
    const sidebar = await setupSidebar()
    expect(sidebar.isGroupCollapsed('runs', true)).toBe(false)
  })

  it('a stored preference wins over the default', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ triggers: false }))
    const sidebar = await setupSidebar()

    expect(sidebar.isGroupCollapsed('triggers', true)).toBe(false)
  })

  it('keeps group prefs independent of one another', async () => {
    const sidebar = await setupSidebar()
    sidebar.toggleGroup('runs', true)
    sidebar.toggleGroup('agents', false)
    expect(sidebar.isGroupCollapsed('runs', true)).toBe(false)
    expect(sidebar.isGroupCollapsed('agents', false)).toBe(true)
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
