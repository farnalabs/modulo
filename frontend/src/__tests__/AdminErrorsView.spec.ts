import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'

const getMock = vi.fn().mockResolvedValue({ items: [], total: 0 })

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(async (...args: unknown[]) => ({ data: await getMock(...args), error: undefined })),
    PATCH: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

vi.mock('../lib/api/schema', () => ({}))

const routerMocks = vi.hoisted(() => ({
  push: vi.fn().mockResolvedValue(undefined),
}))

vi.mock('vue-router', () => ({
  useRoute: vi.fn(() => ({
    path: '/admin/errors',
    fullPath: '/admin/errors',
    params: {},
    query: {},
    hash: '',
    matched: [],
    name: 'admin-errors',
    redirectedFrom: undefined,
    meta: {},
  })),
  useRouter: vi.fn(() => ({
    push: routerMocks.push,
    replace: vi.fn(),
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

import AdminErrorsView from '../views/AdminErrorsView.vue'
import { api } from '../lib/api/client'

function mountView() {
  return mount(AdminErrorsView, {
    global: {
      stubs: {
        FeatureGate: { template: '<div><slot /></div>' },
      },
    },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  getMock.mockResolvedValue({ items: [], total: 0 })
})

afterEach(() => {
  vi.useRealTimers()
})

const LONG_MESSAGE =
  'Traceback (most recent call last): ' + 'x'.repeat(400) + ' ValueError: pipeline output schema mismatch'

function mountWithLongMessageGroup() {
  getMock.mockImplementation(async (path: string) => {
    if (path === '/api/v1/errors') {
      return {
        items: [
          {
            id: 'eg-long-1',
            level_peak: 'error',
            sample_message: LONG_MESSAGE,
            count: 3,
            first_seen: new Date().toISOString(), // nosemgrep: new-date-without-guard
            last_seen: new Date().toISOString(), // nosemgrep: new-date-without-guard
            status: 'new',
            assigned_to: null,
          },
        ],
        total: 1,
      }
    }
    return { items: [], total: 0 }
  })
  return mountView()
}

describe('AdminErrorsView', () => {
  it('renders without crashing and shows the empty state', async () => {
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('No error groups found')
    wrapper.unmount()
  })

  it('renders the scheduler-starvation banner when the starvation endpoint reports starved pipelines', async () => {
    getMock.mockImplementation(async (path: string) => {
      if (path === '/api/v1/errors/scheduler-starvation') {
        return {
          items: [
            {
              pipeline_id: '11111111-1111-4111-8111-111111111111',
              pipeline_name: 'Starved Pipeline',
              pending_count: 63,
              oldest_created_at: new Date(Date.now() - 13 * 3600 * 1000).toISOString(), // nosemgrep: new-date-without-guard
              oldest_age_minutes: 780,
            },
          ],
          total: 1,
          threshold_minutes: 10,
        }
      }
      return { items: [], total: 0 }
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const banner = wrapper.find('[data-testid="scheduler-starvation"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('Starved Pipeline')
    expect(banner.text()).toContain('63')
    expect(api.GET).toHaveBeenCalledWith('/api/v1/errors/scheduler-starvation')
    wrapper.unmount()
  })

  it('hides the scheduler-starvation banner when no pipeline is starved', async () => {
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="scheduler-starvation"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('re-polls the scheduler-starvation endpoint every 60 seconds', async () => {
    // The banner monitors a LIVE incident — a mount-only fetch (staleTime 30s,
    // refetchOnWindowFocus off) would freeze exactly during the long wedge it
    // exists to surface. The view re-polls every 60s.
    getMock.mockImplementation(async (path: string) => {
      if (path === '/api/v1/errors/scheduler-starvation') {
        return { items: [], total: 0, threshold_minutes: 10 }
      }
      return { items: [], total: 0 }
    })
    vi.useFakeTimers()
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const starvationCalls = () =>
      (api.GET as unknown as ReturnType<typeof vi.fn>).mock.calls.filter(
        ([path]) => path === '/api/v1/errors/scheduler-starvation',
      ).length
    const before = starvationCalls()
    expect(before).toBeGreaterThanOrEqual(1)

    vi.advanceTimersByTime(60_000)
    await flushPromises()
    await nextTick()

    expect(starvationCalls()).toBeGreaterThan(before)
    wrapper.unmount()
  })

  it('reloads error groups with the search term after typing (debounced), resetting to page 1', async () => {
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const searchInput = wrapper.find('[data-testid="filter-bar-search"]')
    expect(searchInput.exists()).toBe(true)

    vi.useFakeTimers()
    await searchInput.setValue('foo')
    await nextTick()

    expect(api.GET).not.toHaveBeenCalledWith('/api/v1/errors', expect.objectContaining({
      params: { query: expect.objectContaining({ search: 'foo' }) },
    }))

    vi.advanceTimersByTime(300)
    vi.useRealTimers()
    await flushPromises()
    await nextTick()

    expect(api.GET).toHaveBeenCalledWith('/api/v1/errors', expect.objectContaining({
      params: { query: expect.objectContaining({ search: 'foo' }) },
    }))
    wrapper.unmount()
  })

  it('truncates the message column by default (block-level ellipsis, bounded width)', async () => {
    const wrapper = mountWithLongMessageGroup()
    await flushPromises()
    await nextTick()

    const truncated = wrapper.find('[data-testid="admin-errors-message-truncated"]')
    expect(truncated.exists()).toBe(true)
    // The inline-span bug: truncate only applies to block-level boxes, so the
    // element must carry truncate + a max-width bound (flex item is blockified).
    expect(truncated.classes()).toContain('truncate')
    expect(truncated.classes()).toContain('max-w-xs')
    expect(truncated.classes()).not.toContain('whitespace-normal')
    expect(truncated.text()).toBe(LONG_MESSAGE)

    const full = wrapper.find('[data-testid="admin-errors-message-full"]')
    expect(full.exists()).toBe(false)
    wrapper.unmount()
  })

  it('expands the full message per row via the toggle button, then collapses', async () => {
    const wrapper = mountWithLongMessageGroup()
    await flushPromises()
    await nextTick()

    const toggle = wrapper.find('[data-testid="admin-errors-expand-eg-long-1"]')
    expect(toggle.exists()).toBe(true)
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(toggle.attributes('aria-label')).toBe('Expand error message')

    await toggle.trigger('click')
    await nextTick()

    expect(wrapper.find('[data-testid="admin-errors-message-truncated"]').exists()).toBe(false)
    const full = wrapper.find('[data-testid="admin-errors-message-full"]')
    expect(full.exists()).toBe(true)
    // Expanded message wraps within the bounded column instead of stretching it.
    expect(full.classes()).toContain('whitespace-normal')
    expect(full.classes()).toContain('break-words')
    expect(full.classes()).toContain('max-w-xs')
    expect(full.text()).toBe(LONG_MESSAGE)
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(toggle.attributes('aria-label')).toBe('Collapse error message')

    await toggle.trigger('click')
    await nextTick()

    expect(wrapper.find('[data-testid="admin-errors-message-full"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="admin-errors-message-truncated"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('does not navigate when the expand button is clicked, but row click still navigates', async () => {
    const wrapper = mountWithLongMessageGroup()
    await flushPromises()
    await nextTick()

    const toggle = wrapper.find('[data-testid="admin-errors-expand-eg-long-1"]')
    await toggle.trigger('click')
    await nextTick()
    expect(routerMocks.push).not.toHaveBeenCalled()

    const row = wrapper.find('tbody tr')
    await row.trigger('click')
    expect(routerMocks.push).toHaveBeenCalledTimes(1)
    expect(routerMocks.push).toHaveBeenCalledWith('/admin/errors/eg-long-1')
    wrapper.unmount()
  })

  it('does not trigger row navigation when pressing a key on the expand button', async () => {
    const wrapper = mountWithLongMessageGroup()
    await flushPromises()
    await nextTick()

    const toggle = wrapper.find('[data-testid="admin-errors-expand-eg-long-1"]')
    // The DataTable row forwards keydown (Enter/Space) to row navigation; the
    // button must stop propagation so keyboard activation stays local. The
    // explicit key payload matches DataTable's `event.key === 'Enter'` check.
    await toggle.trigger('keydown', { key: 'Enter' })
    await nextTick()
    expect(routerMocks.push).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('truncates and expands a null message as the (no message) fallback', async () => {
    getMock.mockImplementation(async (path: string) => {
      if (path === '/api/v1/errors') {
        return {
          items: [
            {
              id: 'eg-empty-1',
              level_peak: 'error',
              sample_message: null,
              count: 1,
              first_seen: new Date().toISOString(), // nosemgrep: new-date-without-guard
              last_seen: new Date().toISOString(), // nosemgrep: new-date-without-guard
              status: 'new',
              assigned_to: null,
            },
          ],
          total: 1,
        }
      }
      return { items: [], total: 0 }
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const truncated = wrapper.find('[data-testid="admin-errors-message-truncated"]')
    expect(truncated.exists()).toBe(true)
    expect(truncated.classes()).toContain('truncate')
    expect(truncated.text()).toBe('(no message)')

    await wrapper.find('[data-testid="admin-errors-expand-eg-empty-1"]').trigger('click')
    await nextTick()

    const full = wrapper.find('[data-testid="admin-errors-message-full"]')
    expect(full.exists()).toBe(true)
    expect(full.classes()).toContain('whitespace-normal')
    expect(full.text()).toBe('(no message)')
    wrapper.unmount()
  })

  it('keeps the table wrapper non-stretching (overflow-x-auto + w-full, no forced min width)', async () => {
    const wrapper = mountWithLongMessageGroup()
    await flushPromises()
    await nextTick()

    const tableWrapper = wrapper.find('.table-wrapper')
    expect(tableWrapper.exists()).toBe(true)
    expect(tableWrapper.classes()).not.toContain('min-w-max')
    expect(tableWrapper.classes()).not.toContain('w-max')

    const table = tableWrapper.find('table')
    expect(table.classes()).toContain('w-full')
    expect(table.classes()).not.toContain('min-w-max')
    expect(table.classes()).not.toContain('w-max')

    const scrollRoot = tableWrapper.find('div')
    expect(scrollRoot.classes()).toContain('overflow-x-auto')
    wrapper.unmount()
  })
})
