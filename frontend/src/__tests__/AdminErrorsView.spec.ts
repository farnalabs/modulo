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
              oldest_created_at: new Date(Date.now() - 13 * 3600 * 1000).toISOString(),
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
})
