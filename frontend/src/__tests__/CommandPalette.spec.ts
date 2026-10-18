import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { useRouter } from 'vue-router'
import CommandPalette from '../components/CommandPalette.vue'
import { clearRecentVisits, getRecentVisits } from '../lib/commandPalette/recents'
import { resetPaletteProviderCaches } from '../lib/commandPalette/providers'

// FAR-53: the palette now consults user-data providers, so the shared API
// client and the runs fetcher are mocked. Defaults return empty pages; tests
// override per-case.
const { apiGetMock, fetchRunsMock } = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  fetchRunsMock: vi.fn(),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: apiGetMock,
    POST: vi.fn(),
    PUT: vi.fn(),
    PATCH: vi.fn(),
    DELETE: vi.fn(),
  },
  getAccessToken: vi.fn(() => null),
  isDemoSession: vi.fn(() => false),
  clearAccessToken: vi.fn(),
  onAuthChange: vi.fn(() => vi.fn()),
}))

vi.mock('../lib/api/runs', () => ({
  fetchRuns: fetchRunsMock,
}))

// Track mounted wrappers so afterEach can unmount them. CommandPalette is
// mounted with attachTo: document.body, so each wrapper must be explicitly
// unmounted to detach its document-level keydown listener between tests.
const mounted: VueWrapper[] = []

function emptyPage() {
  return { data: { items: [], total: 0 }, error: undefined, response: { status: 200 } }
}

beforeEach(() => {
  vi.clearAllMocks()
  // CommandPalette uses useNavVisibilityContext, which depends on the plan
  // Pinia store, so a Pinia instance must be active during mount.
  setActivePinia(createPinia())
  // jsdom does not implement scrollIntoView; the selectedIndex watcher calls
  // it whenever the selection changes.
  Element.prototype.scrollIntoView = vi.fn()
  apiGetMock.mockResolvedValue(emptyPage())
  fetchRunsMock.mockResolvedValue({ items: [] })
  clearRecentVisits()
  resetPaletteProviderCaches()
})

async function openPalette() {
  const wrapper = mount(CommandPalette, { attachTo: document.body })
  mounted.push(wrapper)
  ;(wrapper.vm as unknown as { open: () => void }).open()
  await nextTick()
  await nextTick()
  return wrapper
}

function paletteInput(): HTMLInputElement {
  const input = document.querySelector<HTMLInputElement>('#commandpalette-search-input')
  expect(input).not.toBeNull()
  return input!
}

function resultButtons(): HTMLElement[] {
  return Array.from(document.querySelectorAll('[data-cmdk-container] button')) as HTMLElement[]
}

function highlightedButton(): HTMLElement | null {
  return resultButtons().find(b => b.classList.contains('bg-accent')) ?? null
}

async function typeQuery(value: string) {
  const input = paletteInput()
  input.value = value
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
  await nextTick()
}

// Real timers: the palette's provider debounce is 250ms, so waiting a little
// longer than that lets the single scheduled search settle.
async function settleSearch() {
  await new Promise((resolve) => setTimeout(resolve, 350))
  await nextTick()
  await nextTick()
}

describe('CommandPalette', () => {
  afterEach(() => {
    vi.useRealTimers()
    while (mounted.length) mounted.pop()?.unmount()
    document.body.innerHTML = ''
  })

  it('clamps selectedIndex when the query shrinks the result list', async () => {
    await openPalette()
    const allItems = resultButtons()
    expect(allItems.length).toBeGreaterThan(3)

    // Move the selection into the middle of the full list.
    const input = paletteInput()
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await nextTick()
    const beforeShrink = highlightedButton()
    expect(beforeShrink).not.toBeNull()

    // Shrink the list to a single result far above the current selection.
    // "Pipelines" is a curated extra in the palette that narrows to 1 hit.
    await typeQuery('pipelines')
    const shrunk = resultButtons()
    expect(shrunk).toHaveLength(1)
    const highlighted = highlightedButton()
    // Regression: without the clamping watcher the selection stays out of
    // range (index 2 of a 1-item list) and nothing is highlighted.
    expect(highlighted).not.toBeNull()
  })

  it('resets selection to 0 when the filtered list empties', async () => {
    await openPalette()
    const input = paletteInput()
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await nextTick()

    // Empty the list.
    await typeQuery('zzzz-no-such-page')
    expect(resultButtons()).toHaveLength(0)

    // Clearing the query restores the full list; selection must be back at 0
    // (before the fix it stayed at index 2 and highlighted the 3rd item).
    await typeQuery('')
    const highlighted = highlightedButton()
    expect(highlighted).not.toBeNull()
    expect(resultButtons().indexOf(highlighted!)).toBe(0)
  })

  it('closes on Escape via the document-level keydown handler', async () => {
    await openPalette()
    expect(paletteInput()).not.toBeNull()

    // The new Escape branch in handleKeydown must close the palette.
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()

    // With isOpen cleared the dialog (and its search input) is unmounted.
    expect(document.querySelector('#commandpalette-search-input')).toBeNull()
  })

  it('closes on Escape via the overlay backdrop keydown handler', async () => {
    await openPalette()
    expect(paletteInput()).not.toBeNull()

    // The template @keydown.escape="close" binding on the backdrop overlay.
    const backdrop = document.querySelector('.fixed.inset-0.bg-black\\/50') as HTMLElement | null
    expect(backdrop).not.toBeNull()
    backdrop!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()

    expect(document.querySelector('#commandpalette-search-input')).toBeNull()
  })
})

describe('CommandPalette user-data search (FAR-53)', () => {
  afterEach(() => {
    while (mounted.length) mounted.pop()?.unmount()
    document.body.innerHTML = ''
  })

  function mockPipelines(items: unknown[], status = 200) {
    apiGetMock.mockImplementation((url: string) => {
      if (url === '/api/v1/pipelines') {
        return Promise.resolve({ data: { items }, error: undefined, response: { status } })
      }
      return Promise.resolve(emptyPage())
    })
  }

  it('debounces rapid typing into a single request per source', async () => {
    await openPalette()
    await typeQuery('p')
    await typeQuery('pi')
    await typeQuery('pin')
    await settleSearch()

    // Only the settled query hits the server — not one request per keystroke.
    expect(fetchRunsMock).toHaveBeenCalledTimes(1)
    expect(fetchRunsMock).toHaveBeenCalledWith({ search: 'pin', page_size: 20 })
    const pipelinePaths = apiGetMock.mock.calls
      .map((call) => call[0])
      .filter((url) => url === '/api/v1/pipelines')
    expect(pipelinePaths).toHaveLength(1)
    apiGetMock.mock.calls.forEach((call) => {
      expect(call[1]).toEqual({ params: { query: { page_size: 100 } } })
    })
  })

  it('shows a loading state while a provider request is in flight', async () => {
    let releaseRuns: (value: unknown) => void = () => {}
    fetchRunsMock.mockReturnValue(new Promise((resolve) => { releaseRuns = resolve }))

    await openPalette()
    await typeQuery('run')
    await new Promise((resolve) => setTimeout(resolve, 300))
    await nextTick()

    expect(document.body.textContent).toContain('Searching...')

    releaseRuns({ items: [] })
    await flushPromises()
    await nextTick()
    expect(document.body.textContent).not.toContain('Searching...')
  })

  it('renders an error state without breaking navigation when every provider fails', async () => {
    apiGetMock.mockResolvedValue({
      data: undefined,
      error: { detail: 'boom' },
      response: { status: 500 },
    })
    fetchRunsMock.mockRejectedValue(new Error('boom'))

    await openPalette()
    await typeQuery('runs')
    await settleSearch()

    const alert = document.querySelector('[role="alert"]')
    expect(alert).not.toBeNull()
    // The nav item still resolves and renders — the palette stays usable.
    expect(resultButtons().map((b) => b.textContent)).toContain('RunsBUILD')
  })

  it('shows the empty state when nothing matches', async () => {
    await openPalette()
    await typeQuery('zzzz-no-such-item')
    await settleSearch()

    expect(resultButtons()).toHaveLength(0)
    expect(document.body.textContent).toContain('No results found')
  })

  it('records the current route as a recent visit and surfaces it on open', async () => {
    await openPalette()
    await flushPromises()
    await nextTick()

    const visits = getRecentVisits()
    expect(visits.map((visit) => visit.path)).toContain('/')
    expect(visits[0]?.label).toBe('Dashboard')

    // The recents provider runs on the empty query, so the jump-back entry is
    // the first row when the palette opens.
    expect(resultButtons()[0]?.textContent).toContain('Dashboard')
  })

  it('navigates over mixed user-data and nav results with the keyboard', async () => {
    mockPipelines([{ id: 'p1', name: 'Payments', description: 'pay flow' }])
    fetchRunsMock.mockResolvedValue({
      items: [{ run_id: 'r1', run_number: 7, pipeline_name: 'Payments' }],
    })
    const router = useRouter() as unknown as { push: ReturnType<typeof vi.fn> }

    await openPalette()
    await typeQuery('pay')
    await settleSearch()

    const labels = resultButtons().map((b) => b.textContent ?? '')
    expect(labels[0]).toContain('Payments')
    expect(labels[0]).toContain('Pipelines')
    expect(labels[1]).toContain('#7')
    expect(labels[1]).toContain('Runs')

    const input = paletteInput()
    // Down twice: past the pipeline onto the run, then open it.
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await nextTick()
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await nextTick()

    expect(router.push).toHaveBeenCalledWith('/runs/r1')
  })

  it('exposes the selected row via aria-activedescendant', async () => {
    await openPalette()
    const input = paletteInput()
    expect(input.getAttribute('aria-activedescendant')).toBe('cmdk-option-0')
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await nextTick()
    expect(input.getAttribute('aria-activedescendant')).toBe('cmdk-option-1')
  })
})
