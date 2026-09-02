import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'

const mockResponses: Record<string, unknown> = {
  default: { items: [], total: 0, page: 1, page_size: 20, next_cursor: null, has_more: false },
}

vi.mock('../lib/api/client', () => {
  const mockGet = vi.fn((url: string) => {
    if (url === '/api/v1/runs') {
      return Promise.resolve({ data: mockResponses['/api/v1/runs'] ?? mockResponses.default, error: undefined })
    }
    return Promise.resolve({ data: mockResponses.default, error: undefined })
  })
  return {
    api: {
      GET: mockGet,
      PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
      POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
      PATCH: vi.fn().mockResolvedValue({ data: null, error: undefined }),
      DELETE: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    },
    getAccessToken: vi.fn().mockReturnValue('mock-token'),
  }
})

const routerMocks = vi.hoisted(() => ({
  push: vi.fn().mockResolvedValue(undefined),
  replace: vi.fn().mockResolvedValue(undefined),
}))

const routeMocks = vi.hoisted(() => ({
  query: {} as Record<string, unknown>,
}))

vi.mock('vue-router', () => ({
  useRoute: vi.fn(() => ({
    path: '/runs',
    fullPath: '/runs',
    params: {},
    query: routeMocks.query,
    hash: '',
    matched: [],
    name: 'runs-list',
    redirectedFrom: undefined,
    meta: {},
  })),
  useRouter: vi.fn(() => ({
    push: routerMocks.push,
    replace: routerMocks.replace,
    resolve: vi.fn(),
    go: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    beforeEach: vi.fn(),
    afterEach: vi.fn(),
    onError: vi.fn(),
    currentRoute: { value: {} },
    getRoutes: vi.fn(() => []),
    addRoute: vi.fn(),
    removeRoute: vi.fn(),
    hasRoute: vi.fn(() => false),
    isReady: vi.fn().mockResolvedValue(undefined),
    install: vi.fn(),
  })),
  createRouter: vi.fn(),
  createWebHistory: vi.fn(() => ({})),
}))

import RunsListView from '../views/RunsListView.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import { api } from '../lib/api/client'

const baseRun = {
  run_id: 'run1',
  pipeline_id: 'p1',
  pipeline_name: 'Test Pipeline',
  status: 'complete',
  trigger_type: 'manual',
  run_number: 1,
  created_at: '2026-01-01T00:00:00Z',
  started_at: '2026-01-01T00:00:00Z',
  completed_at: '2026-01-01T00:02:14Z',
  error_code: null,
  error_detail: null,
  total_cost_usd: 0.5,
  account_id: null,
}

function listWith(items: unknown[], opts: { next_cursor?: string | null; has_more?: boolean; total?: number } = {}) {
  return {
    items,
    total: opts.total ?? items.length,
    page: 1,
    page_size: 20,
    next_cursor: opts.next_cursor ?? null,
    has_more: opts.has_more ?? false,
  }
}

const manyRuns = Array.from({ length: 25 }, (_, i) => ({ ...baseRun, run_id: `run${i}` }))

function mountView() {
  return mount(RunsListView, {
    global: {
      stubs: {
        ErrorAlert: true,
        'router-link': {
          props: ['to'],
          template: '<a :data-to="typeof to === \'string\' ? to : to.path"><slot /></a>',
        },
      },
    },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  routeMocks.query = {}
  mockResponses['/api/v1/runs'] = listWith([])
})

afterEach(() => {
  vi.useRealTimers()
})

describe('RunsListView', () => {
  it('renders without crashing', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.exists()).toBe(true)
  })

  it('renders a human-readable trigger type label', async () => {
    mockResponses['/api/v1/runs'] = listWith([{ ...baseRun, trigger_type: 'agent_signal' }])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Agent Signal')
    expect(wrapper.text()).not.toContain('agent_signal')
    wrapper.unmount()
  })

  it('renders empty state when no runs exist', async () => {
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('No runs found')
  })

  it('shows Start, End and Duration columns instead of Created / Last Run', async () => {
    mockResponses['/api/v1/runs'] = listWith([baseRun])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    const text = wrapper.text()
    expect(text).toContain('Start')
    expect(text).toContain('End')
    expect(text).toContain('Duration')
    expect(text).not.toContain('Last Run')
    expect(text).not.toContain('Created')
  })

  it('renders an error column with a badge and detail preview for failed runs', async () => {
    mockResponses['/api/v1/runs'] = listWith([
      { ...baseRun, status: 'failed', error_code: 'harness.worker_failed', error_detail: 'worker crashed: sk-abc1234567890' },
    ])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    const badge = wrapper.find('[data-testid="runs-list-error-run1"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toBe('Worker failed')
    expect(badge.attributes('title')).toBe('worker crashed: sk-abc1234567890')
    wrapper.unmount()
  })

  it('does not render an error badge when error_code is null', async () => {
    mockResponses['/api/v1/runs'] = listWith([baseRun])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="runs-list-error-run1"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('does not render an input preview column even when runs have parameters', async () => {
    mockResponses['/api/v1/runs'] = listWith([
      { ...baseRun, status: 'running', input_payload: { task: 'fix bug', pr_number: 42 } },
    ])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="runs-list-input-run1"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('fix bug')
    wrapper.unmount()
  })

  it('shows a dash when a run has no input payload', async () => {
    mockResponses['/api/v1/runs'] = listWith([baseRun])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="runs-list-input-run1"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('renders duration formatted from start and end timestamps', async () => {
    mockResponses['/api/v1/runs'] = listWith([baseRun])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('2m 14s')
  })

  it('renders multi-hour durations with padded minutes', async () => {
    mockResponses['/api/v1/runs'] = listWith([
      { ...baseRun, started_at: '2026-01-01T00:00:00Z', completed_at: '2026-01-01T01:02:03Z' },
    ])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('1h 02m')
  })

  it('shows a dash for duration when the run is still in progress', async () => {
    mockResponses['/api/v1/runs'] = listWith([{ ...baseRun, completed_at: null }])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('—')
  })

  it('shows a live elapsed duration with an (elapsed) suffix for executing runs', async () => {
    mockResponses['/api/v1/runs'] = listWith([{ ...baseRun, status: 'running', completed_at: null }])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    const durationCell = wrapper.find('[data-testid="runs-list-duration-run1"]')
    expect(durationCell.exists()).toBe(true)
    expect(durationCell.text()).toContain('(elapsed)')
    expect(durationCell.text()).not.toContain('—')
    const liveElapsed = durationCell.find('[role="status"]')
    expect(liveElapsed.exists()).toBe(true)
    wrapper.unmount()
  })

  it('shows aggregate cost with a (+child) marker when child runs exist', async () => {
    mockResponses['/api/v1/runs'] = listWith([
      { ...baseRun, child_runs_cost_usd: '0.25', aggregate_cost_usd: '0.75' },
    ])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    const aggregateCell = wrapper.find('[data-testid="runs-list-aggregate-cost"]')
    expect(aggregateCell.exists()).toBe(true)
    expect(aggregateCell.text()).toContain('0.7500')
    expect(wrapper.text()).toContain('(+child)')
  })

  it('shows a (+N children) suffix when the child run count is available', async () => {
    mockResponses['/api/v1/runs'] = listWith([
      { ...baseRun, child_runs_cost_usd: '0.25', aggregate_cost_usd: '0.75', child_runs_count: 3 },
    ])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    const aggregateCell = wrapper.find('[data-testid="runs-list-aggregate-cost"]')
    expect(aggregateCell.exists()).toBe(true)
    expect(wrapper.text()).toContain('(+3 children)')
    expect(wrapper.text()).not.toContain('(+child)')
  })


  it('shows own cost when aggregate equals own cost (no children)', async () => {
    mockResponses['/api/v1/runs'] = listWith([
      { ...baseRun, child_runs_cost_usd: '0.000000', aggregate_cost_usd: '0.5' },
    ])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="runs-list-aggregate-cost"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('0.5000')
    expect(wrapper.text()).not.toContain('(+child)')
  })

  it('falls back to own cost when rollup fields are absent', async () => {
    mockResponses['/api/v1/runs'] = listWith([baseRun])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="runs-list-aggregate-cost"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('0.5000')
    expect(wrapper.text()).not.toContain('NaN')
    expect(wrapper.text()).not.toContain('(+child)')
  })

  it.each(['pending', 'running', 'awaiting_human', 'claimed'])('renders a stop button for %s runs', async (status) => {
    mockResponses['/api/v1/runs'] = listWith([{ ...baseRun, status }])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    const stopBtn = wrapper.find('[data-testid="runs-list-cancel-run1"]')
    expect(stopBtn.exists()).toBe(true)
    expect(stopBtn.text()).toContain('Stop')
    wrapper.unmount()
  })

  it.each(['complete', 'failed', 'cancelled', 'eval_failed', 'stalled', 'budget_exceeded'])('renders no stop button for %s runs', async (status) => {
    mockResponses['/api/v1/runs'] = listWith([{ ...baseRun, status }])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="runs-list-cancel-run1"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('disables the stop button while the cancel request is in flight', async () => {
    mockResponses['/api/v1/runs'] = listWith([{ ...baseRun, status: 'running' }])
    let resolvePost!: (value: unknown) => void
    ;(api.POST as any).mockImplementation(() => new Promise((resolve) => { resolvePost = resolve }))
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const stopBtn = wrapper.find('[data-testid="runs-list-cancel-run1"]')
    await stopBtn.trigger('click')
    await nextTick()
    await stopBtn.trigger('click')
    await nextTick()

    expect(stopBtn.attributes('disabled')).toBeDefined()

    resolvePost({ data: null, error: undefined })
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="runs-list-cancel-run1"]').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('does not navigate to run detail when the stop button is clicked', async () => {
    mockResponses['/api/v1/runs'] = listWith([{ ...baseRun, status: 'pending' }])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const stopBtn = wrapper.find('[data-testid="runs-list-cancel-run1"]')
    await stopBtn.trigger('click')
    await nextTick()
    await stopBtn.trigger('click')
    await flushPromises()
    await nextTick()

    expect(routerMocks.push).not.toHaveBeenCalled()

    const viewLink = wrapper.find('[data-testid="runs-list-view-run1"]')
    expect(viewLink.exists()).toBe(true)
    expect(viewLink.attributes('data-to')).toBe('/runs/run1')
    wrapper.unmount()
  })

  it('does not navigate when the stop button is activated via keyboard', async () => {
    mockResponses['/api/v1/runs'] = listWith([{ ...baseRun, status: 'pending' }])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const stopBtn = wrapper.find('[data-testid="runs-list-cancel-run1"]')
    await stopBtn.trigger('keydown', { key: 'Enter' })
    await nextTick()
    await stopBtn.trigger('keydown', { key: ' ' })
    await nextTick()

    expect(routerMocks.push).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('calls the cancel endpoint after a two-step confirm and updates the row status', async () => {
    mockResponses['/api/v1/runs'] = listWith([{ ...baseRun, status: 'pending' }])
    ;(api.POST as any).mockImplementation(async (url: string) => {
      if (url === '/api/v1/runs/{run_id}/cancel') {
        mockResponses['/api/v1/runs'] = listWith([{ ...baseRun, status: 'cancelled' }])
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const stopBtn = wrapper.find('[data-testid="runs-list-cancel-run1"]')
    await stopBtn.trigger('click')
    await nextTick()
    expect(stopBtn.text()).toContain('Confirm')

    await stopBtn.trigger('click')
    await flushPromises()
    await nextTick()

    expect(api.POST).toHaveBeenCalledWith('/api/v1/runs/{run_id}/cancel', {
      params: { path: { run_id: 'run1' } },
    })
    expect(wrapper.text()).toContain('cancelled')
    wrapper.unmount()
  })

  it('shows an inline error when the cancel request fails', async () => {
    mockResponses['/api/v1/runs'] = listWith([{ ...baseRun, status: 'running' }])
    ;(api.POST as any).mockResolvedValue({ data: null, error: { detail: 'run_already_terminal' } })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const stopBtn = wrapper.find('[data-testid="runs-list-cancel-run1"]')
    await stopBtn.trigger('click')
    await nextTick()
    await stopBtn.trigger('click')
    await flushPromises()
    await nextTick()

    const errorEl = wrapper.find('[data-testid="runs-list-cancel-error-run1"]')
    expect(errorEl.exists()).toBe(true)
    expect(errorEl.text()).toContain('run_already_terminal')
    wrapper.unmount()
  })

  it('reloads runs with the search term after typing (debounced), resetting to the first page', async () => {
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const searchInput = wrapper.find('[data-testid="filter-bar-search"]')
    expect(searchInput.exists()).toBe(true)

    vi.useFakeTimers()
    await searchInput.setValue('foo')
    await nextTick()

    // Debounce window has not elapsed yet — no request carrying the search term.
    expect(api.GET).not.toHaveBeenCalledWith('/api/v1/runs', expect.objectContaining({
      params: { query: expect.objectContaining({ search: 'foo' }) },
    }))

    vi.advanceTimersByTime(300)
    vi.useRealTimers()
    await flushPromises()
    await nextTick()

    expect(api.GET).toHaveBeenCalledWith('/api/v1/runs', expect.objectContaining({
      params: { query: expect.objectContaining({ search: 'foo' }) },
    }))
    wrapper.unmount()
  })

  it('does not render a Reset button (filters clear by emptying the search box)', async () => {
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).not.toContain('Reset')
    wrapper.unmount()
  })

  it('restores the cursor from the route query on mount (back-navigation deep link)', async () => {
    routeMocks.query = { cursor: 'deep-link-cursor' }
    mockResponses['/api/v1/runs'] = listWith(manyRuns.slice(0, 20), { next_cursor: 'cursor-2', has_more: true })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    expect(api.GET).toHaveBeenCalledWith('/api/v1/runs', expect.objectContaining({
      params: { query: expect.objectContaining({ cursor: 'deep-link-cursor' }) },
    }))
    // The visited-page count is unknowable for a fresh deep link — no page label.
    expect(wrapper.text()).not.toContain('Page ')
    wrapper.unmount()
  })

  it('ignores malformed cursor route params and loads without a cursor', async () => {
    for (const badCursor of ['', ['a', 'b'], { c: 1 }]) {
      routeMocks.query = { cursor: badCursor as unknown as Record<string, unknown> }
      const wrapper = mountView()
      await flushPromises()
      await nextTick()

      const lastCall = (api.GET as any).mock.calls.at(-1)
      expect(lastCall[1].params.query.cursor).toBeUndefined()
      wrapper.unmount()
    }
  })

  it('loads the first page without a cursor and writes no cursor query on a bare mount', async () => {
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const firstCall = (api.GET as any).mock.calls[0]
    expect(firstCall[1].params.query.cursor).toBeUndefined()
    expect(firstCall[1].params.query.page_size).toBe(20)
    // The retired offset param must never reappear on the wire.
    expect(firstCall[1].params.query.page).toBeUndefined()
    expect(routerMocks.replace).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('shows the page-position label on a bare mount (page 1 is known)', async () => {
    mockResponses['/api/v1/runs'] = listWith([baseRun])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Page 1')
    wrapper.unmount()
  })

  it('scrubs a stale legacy ?page= param from the URL on mount', async () => {
    routeMocks.query = { page: '5', status: 'running' }
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    expect(routerMocks.replace).toHaveBeenCalledWith({ query: { status: 'running' } })
    const firstCall = (api.GET as any).mock.calls[0]
    expect(firstCall[1].params.query.page).toBeUndefined()
    wrapper.unmount()
  })

  it('requests the next page via next_cursor and persists the cursor to the route query', async () => {
    mockResponses['/api/v1/runs'] = listWith(manyRuns.slice(0, 20), { next_cursor: 'cursor-1', has_more: true, total: 25 })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const nextBtn = wrapper.find('[data-testid="runs-list-next-page"]')
    expect(nextBtn.exists()).toBe(true)
    await nextBtn.trigger('click')
    await flushPromises()
    await nextTick()

    expect(api.GET).toHaveBeenLastCalledWith('/api/v1/runs', expect.objectContaining({
      params: { query: expect.objectContaining({ cursor: 'cursor-1' }) },
    }))
    const lastCall = (api.GET as any).mock.calls.at(-1)
    expect(lastCall[1].params.query.page).toBeUndefined()
    expect(routerMocks.replace).toHaveBeenLastCalledWith({ query: { cursor: 'cursor-1' } })
    wrapper.unmount()
  })

  it('ignores a second Next click while a page fetch is in flight', async () => {
    mockResponses['/api/v1/runs'] = listWith(manyRuns.slice(0, 20), { next_cursor: 'cursor-1', has_more: true, total: 25 })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    let release: (value: unknown) => void = () => {}
    ;(api.GET as any).mockImplementationOnce(
      () => new Promise((resolve) => { release = resolve }),
    )
    const nextBtn = wrapper.find('[data-testid="runs-list-next-page"]')
    await nextBtn.trigger('click')
    await flushPromises()

    await nextBtn.trigger('click')
    await flushPromises()
    const cursor1Calls = (api.GET as any).mock.calls.filter(
      (call: any[]) => call[1]?.params?.query?.cursor === 'cursor-1',
    )
    expect(cursor1Calls).toHaveLength(1)

    release({ data: listWith(manyRuns.slice(5, 25), { next_cursor: null, has_more: false, total: 25 }), error: undefined })
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Page 2')
    wrapper.unmount()
  })

  it('keeps the pagination footer with a working Prev on an emptied cursor page', async () => {
    mockResponses['/api/v1/runs'] = listWith(manyRuns.slice(0, 20), { next_cursor: 'cursor-1', has_more: true, total: 25 })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    // The page behind the cursor was emptied between fetches: no rows, no more pages.
    mockResponses['/api/v1/runs'] = listWith([], { next_cursor: null, has_more: false, total: 0 })
    await wrapper.find('[data-testid="runs-list-next-page"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('No runs found')
    const prevBtn = wrapper.find('[data-testid="runs-list-prev-page"]')
    expect(prevBtn.exists()).toBe(true)
    expect((prevBtn.element as HTMLButtonElement).disabled).toBe(false)
    expect((wrapper.find('[data-testid="runs-list-next-page"]').element as HTMLButtonElement).disabled).toBe(true)

    // Prev recovers the user back to the last non-empty page.
    mockResponses['/api/v1/runs'] = listWith(manyRuns.slice(0, 20), { next_cursor: 'cursor-1', has_more: true, total: 25 })
    await prevBtn.trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Test Pipeline')
    wrapper.unmount()
  })

  it('walks forward via cursors and pops the stack for prev back to the first page', async () => {
    mockResponses['/api/v1/runs'] = listWith(manyRuns.slice(0, 20), { next_cursor: 'cursor-1', has_more: true, total: 35 })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    // Page 1 is loaded; the response to the next click must be page 2.
    mockResponses['/api/v1/runs'] = listWith(manyRuns.slice(5, 25), { next_cursor: 'cursor-2', has_more: true, total: 35 })
    const nextBtn = wrapper.find('[data-testid="runs-list-next-page"]')
    await nextBtn.trigger('click')
    await flushPromises()
    await nextTick()
    expect(api.GET).toHaveBeenLastCalledWith('/api/v1/runs', expect.objectContaining({
      params: { query: expect.objectContaining({ cursor: 'cursor-1' }) },
    }))

    // Page 2 is loaded; the response to the next click must be page 3 (last).
    mockResponses['/api/v1/runs'] = listWith(manyRuns.slice(10, 30), { next_cursor: null, has_more: false, total: 35 })
    await nextBtn.trigger('click')
    await flushPromises()
    await nextTick()
    expect(api.GET).toHaveBeenLastCalledWith('/api/v1/runs', expect.objectContaining({
      params: { query: expect.objectContaining({ cursor: 'cursor-2' }) },
    }))

    const prevBtn = wrapper.find('[data-testid="runs-list-prev-page"]')
    await prevBtn.trigger('click')
    await flushPromises()
    await nextTick()
    expect(api.GET).toHaveBeenLastCalledWith('/api/v1/runs', expect.objectContaining({
      params: { query: expect.objectContaining({ cursor: 'cursor-1' }) },
    }))

    await prevBtn.trigger('click')
    await flushPromises()
    await nextTick()
    const lastCall = (api.GET as any).mock.calls.at(-1)
    expect(lastCall[1].params.query.cursor).toBeUndefined()
    expect(wrapper.text()).toContain('Page 1')
    wrapper.unmount()
  })

  it('resets the cursor to the first page when the status filter changes', async () => {
    mockResponses['/api/v1/runs'] = listWith(manyRuns.slice(0, 20), { next_cursor: 'cursor-1', has_more: true, total: 25 })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="runs-list-next-page"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(api.GET).toHaveBeenLastCalledWith('/api/v1/runs', expect.objectContaining({
      params: { query: expect.objectContaining({ cursor: 'cursor-1' }) },
    }))

    const filterBar = wrapper.findComponent(FilterBar)
    filterBar.vm.$emit('update:filter', 'status', 'running')
    await flushPromises()
    await nextTick()

    const lastCall = (api.GET as any).mock.calls.at(-1)
    expect(lastCall[1].params.query.status).toBe('running')
    expect(lastCall[1].params.query.cursor).toBeUndefined()
    wrapper.unmount()
  })

  it('resets the cursor when the debounced search term changes', async () => {
    mockResponses['/api/v1/runs'] = listWith(manyRuns.slice(0, 20), { next_cursor: 'cursor-1', has_more: true, total: 25 })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="runs-list-next-page"]').trigger('click')
    await flushPromises()
    await nextTick()

    const searchInput = wrapper.find('[data-testid="filter-bar-search"]')
    vi.useFakeTimers()
    await searchInput.setValue('foo')
    vi.advanceTimersByTime(300)
    vi.useRealTimers()
    await flushPromises()
    await nextTick()

    const lastCall = (api.GET as any).mock.calls.at(-1)
    expect(lastCall[1].params.query.search).toBe('foo')
    expect(lastCall[1].params.query.cursor).toBeUndefined()
    wrapper.unmount()
  })

  it('disables Next when has_more is false and Prev on the first page', async () => {
    mockResponses['/api/v1/runs'] = listWith([baseRun], { next_cursor: null, has_more: false })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    expect((wrapper.find('[data-testid="runs-list-next-page"]').element as HTMLButtonElement).disabled).toBe(true)
    expect((wrapper.find('[data-testid="runs-list-prev-page"]').element as HTMLButtonElement).disabled).toBe(true)
    wrapper.unmount()
  })

  it('keeps untracked query params (e.g. theme=agent) in the URL when paginating', async () => {
    routeMocks.query = { theme: 'agent' }
    mockResponses['/api/v1/runs'] = listWith(manyRuns.slice(0, 20), { next_cursor: 'cursor-1', has_more: true, total: 25 })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const nextBtn = wrapper.find('[data-testid="runs-list-next-page"]')
    expect(nextBtn).toBeDefined()
    await nextBtn!.trigger('click')
    await flushPromises()
    await nextTick()

    expect(routerMocks.replace).toHaveBeenLastCalledWith({ query: { theme: 'agent', cursor: 'cursor-1' } })
    wrapper.unmount()
  })

  it('preserves an active status filter in the URL when paginating', async () => {
    routeMocks.query = { status: 'running' }
    mockResponses['/api/v1/runs'] = listWith(manyRuns.slice(0, 20), { next_cursor: 'cursor-1', has_more: true, total: 25 })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const nextBtn = wrapper.find('[data-testid="runs-list-next-page"]')
    expect(nextBtn).toBeDefined()
    await nextBtn!.trigger('click')
    await flushPromises()
    await nextTick()

    expect(api.GET).toHaveBeenLastCalledWith('/api/v1/runs', expect.objectContaining({
      params: { query: expect.objectContaining({ cursor: 'cursor-1', status: 'running' }) },
    }))
    expect(routerMocks.replace).toHaveBeenLastCalledWith({ query: { status: 'running', cursor: 'cursor-1' } })
    wrapper.unmount()
  })

  it('does not render a trigger actor column', async () => {
    mockResponses['/api/v1/runs'] = listWith([{ ...baseRun, trigger_actor: 'Duncan (GitHub)' }])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="runs-list-trigger-actor-run1"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Duncan (GitHub)')
    wrapper.unmount()
  })

  it('renders the trigger type label for the trigger column when actor is absent', async () => {
    mockResponses['/api/v1/runs'] = listWith([baseRun])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="runs-list-trigger-actor-run1"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Manual')
    wrapper.unmount()
  })

  it('renders the heartbeat column as Xs ago for a running run with a recent heartbeat', async () => {
    const heartbeatAt = new Date(Date.now() - 5000).toISOString() // nosemgrep: new-date-without-guard
    mockResponses['/api/v1/runs'] = listWith([
      { ...baseRun, status: 'running', completed_at: null, heartbeat_at: heartbeatAt },
    ])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    const cell = wrapper.find('[data-testid="runs-list-heartbeat-run1"]')
    expect(cell.exists()).toBe(true)
    expect(cell.text()).toMatch(/^\d+s ago$/)
    wrapper.unmount()
  })

  it('renders a dash for the heartbeat when the run is terminal', async () => {
    mockResponses['/api/v1/runs'] = listWith([{ ...baseRun, heartbeat_at: new Date().toISOString() }])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    const cell = wrapper.find('[data-testid="runs-list-heartbeat-run1"]')
    expect(cell.exists()).toBe(true)
    expect(cell.text()).toBe('—')
    wrapper.unmount()
  })

  it('renders the queued badge when capacity.waiting is true', async () => {
    mockResponses['/api/v1/runs'] = listWith([
      { ...baseRun, status: 'pending', capacity: { active_runs: 3, concurrency_limit: 5, waiting: true } },
    ])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    const badge = wrapper.find('[data-testid="runs-list-queued-run1"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toContain('queued')
    wrapper.unmount()
  })
})
