import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'

const mockResponses: Record<string, unknown> = {
  default: { items: [], total: 0, page: 1, page_size: 20, next_cursor: null, has_more: false },
}

vi.mock('../lib/api/client', () => {
  return {
    api: {
      GET: vi.fn().mockImplementation((url: string) => {
        if (url === '/api/v1/runs') {
          return Promise.resolve({ data: mockResponses['/api/v1/runs'] ?? mockResponses.default, error: undefined })
        }
        return Promise.resolve({ data: mockResponses.default, error: undefined })
      }),
      PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
      POST: vi.fn().mockImplementation((url: string) => {
        if (url === '/api/v1/runs/{run_id}/rerun') {
          if (mockRerunError) return Promise.resolve({ data: null, error: mockRerunError })
          return Promise.resolve({ data: { run_id: 'new-run-id' }, error: undefined })
        }
        return Promise.resolve({ data: null, error: undefined })
      }),
      PATCH: vi.fn().mockResolvedValue({ data: null, error: undefined }),
      DELETE: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    },
    getAccessToken: vi.fn().mockReturnValue('mock-token'),
  }
})

let mockRerunError: unknown = undefined

const routerPush = vi.hoisted(() => vi.fn().mockResolvedValue(undefined))

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
    push: routerPush,
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

import RunsListView from '../views/RunsListView.vue'
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
  mockRerunError = undefined
  localStorage.clear()
  routeMocks.query = {}
  mockResponses['/api/v1/runs'] = listWith([])
})

describe('RunsListView rerun (FAR-788)', () => {
  it.each(['complete', 'failed'])('renders a re-run button for %s runs', async (status) => {
    mockResponses['/api/v1/runs'] = listWith([{ ...baseRun, status }])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    const rerunBtn = wrapper.find('[data-testid="runs-list-rerun-run1"]')
    expect(rerunBtn.exists()).toBe(true)
    expect(rerunBtn.text()).toContain('Re-run')
    wrapper.unmount()
  })

  it.each(['running', 'pending', 'awaiting_human', 'claimed', 'unknown', 'hitl_parked'])('renders no re-run button for %s runs', async (status) => {
    mockResponses['/api/v1/runs'] = listWith([{ ...baseRun, status }])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="runs-list-rerun-run1"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('uses a two-click confirm before rerunning', async () => {
    mockResponses['/api/v1/runs'] = listWith([{ ...baseRun, status: 'complete' }])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const btn = wrapper.find('[data-testid="runs-list-rerun-run1"]')
    await btn.trigger('click')
    await nextTick()
    expect(api.POST).not.toHaveBeenCalledWith('/api/v1/runs/{run_id}/rerun', expect.anything())
    expect(wrapper.find('[data-testid="runs-list-rerun-run1"]').text()).toContain('Re-run?')

    await wrapper.find('[data-testid="runs-list-rerun-run1"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(api.POST).toHaveBeenCalledWith('/api/v1/runs/{run_id}/rerun', {
      params: { path: { run_id: 'run1' } },
    })
    wrapper.unmount()
  })

  it('navigates to the new run detail after a successful rerun', async () => {
    mockResponses['/api/v1/runs'] = listWith([{ ...baseRun, status: 'complete' }])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="runs-list-rerun-run1"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="runs-list-rerun-run1"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(routerPush).toHaveBeenCalledWith('/runs/new-run-id')
    wrapper.unmount()
  })

  it('shows an error and stays on the list when the rerun request fails', async () => {
    mockRerunError = { detail: 'boom' }
    mockResponses['/api/v1/runs'] = listWith([{ ...baseRun, status: 'complete' }])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="runs-list-rerun-run1"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="runs-list-rerun-run1"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="runs-list-rerun-error-run1"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="runs-list-rerun-error-run1"]').text()).toContain('Failed to re-run:')
    expect(routerPush).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
