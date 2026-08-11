import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const STORAGE_KEY = 'sidebar-group-prefs'

beforeEach(() => {
  vi.resetModules()
  localStorage.clear()
})

afterEach(() => {
  localStorage.clear()
})

async function setupSidebar() {
  const mod = await import('../../composables/useSidebar')
  return mod.useSidebar()
}

describe('useSidebar', () => {
  it('isGroupCollapsed falls back to the default when no pref is stored', async () => {
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

  it('keeps group prefs independent of one another', async () => {
    const sidebar = await setupSidebar()
    sidebar.toggleGroup('runs', true)
    sidebar.toggleGroup('agents', false)
    expect(sidebar.isGroupCollapsed('runs', true)).toBe(false)
    expect(sidebar.isGroupCollapsed('agents', false)).toBe(true)
  })

  it('exposes groupPrefs as a readonly object', async () => {
    const sidebar = await setupSidebar()
    sidebar.toggleGroup('runs', true)
    expect(sidebar.groupPrefs.value).toEqual({ runs: false })
  })
})
