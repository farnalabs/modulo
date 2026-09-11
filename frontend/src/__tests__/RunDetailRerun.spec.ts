import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'

let mockRunStatus = 'complete'
let mockGraphNodes: Array<{ idempotent?: boolean }> | null = null
let mockRerunError: unknown = undefined

const testRoute = vi.hoisted(() => ({
  params: { id: 'test-run-id' },
  fullPath: '/runs/test-run-id',
  path: '/runs/test-run-id',
  query: {},
  hash: '',
  matched: [],
  name: 'run-detail',
  redirectedFrom: undefined,
  meta: {},
}))

const routerPush = vi.hoisted(() => vi.fn().mockResolvedValue(undefined))

vi.mock('vue-router', () => {
  const mockRouter = {
    push: routerPush,
    replace: vi.fn(),
    resolve: vi.fn(),
    go: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    beforeEach: vi.fn(),
    afterEach: vi.fn(),
    onError: vi.fn(),
    currentRoute: { value: testRoute },
    getRoutes: vi.fn(() => []),
    addRoute: vi.fn(),
    removeRoute: vi.fn(),
    hasRoute: vi.fn(() => false),
    isReady: vi.fn().mockResolvedValue(undefined),
    install: vi.fn(),
  }
  return {
    useRoute: vi.fn(() => testRoute),
    useRouter: vi.fn(() => mockRouter),
    createRouter: vi.fn(() => mockRouter),
    createWebHistory: vi.fn(() => ({})),
  }
})

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({
          data: {
            run_id: 'test-run-id',
            pipeline_id: 'test-pipeline',
            status: mockRunStatus,
            total_cost_usd: null,
            token_consumption: null,
            node_token_usage: null,
            trace_id: null,
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({ data: { outputs_json: null, input_payload: null }, error: undefined })
      }
      if (url === '/api/v1/pipelines/{pipeline_id}/graph') {
        return Promise.resolve({ data: { nodes: mockGraphNodes ?? [], edges: [] }, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    }),
    POST: vi.fn().mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}/rerun') {
        if (mockRerunError) return Promise.resolve({ data: null, error: mockRerunError })
        return Promise.resolve({ data: { run_id: 'new-run-id' }, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import RunDetailView from '../views/RunDetailView.vue'
import { api } from '../lib/api/client'

function mountView() {
  return mount(RunDetailView)
}

beforeEach(() => {
  vi.clearAllMocks()
  mockRunStatus = 'complete'
  mockGraphNodes = null
  mockRerunError = undefined
})

describe('RunDetailView rerun (FAR-788)', () => {
  it('shows the re-run button for a completed run', async () => {
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    const btn = wrapper.find('[data-testid="run-rerun"]')
    expect(btn.exists()).toBe(true)
    expect(btn.text()).toContain('Re-run')
    wrapper.unmount()
  })

  it('shows the re-run button for a failed run', async () => {
    mockRunStatus = 'failed'
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="run-rerun"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('hides the re-run button for a running run', async () => {
    mockRunStatus = 'running'
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="run-rerun"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('fires the rerun request directly when every graph node is idempotent', async () => {
    mockGraphNodes = [{ idempotent: true }]
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="run-rerun"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(api.POST).toHaveBeenCalledWith('/api/v1/runs/{run_id}/rerun', {
      params: { path: { run_id: 'test-run-id' } },
    })
    expect(routerPush).toHaveBeenCalledWith('/runs/new-run-id')
    wrapper.unmount()
  })

  it('requires a confirm click with a warning when any graph node is not idempotent', async () => {
    mockGraphNodes = [{ idempotent: true }, { idempotent: false }]
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const btn = wrapper.find('[data-testid="run-rerun"]')
    await btn.trigger('click')
    await nextTick()

    // First click only arms the confirm — no request fired yet.
    expect(api.POST).not.toHaveBeenCalledWith('/api/v1/runs/{run_id}/rerun', expect.anything())
    expect(wrapper.find('[data-testid="run-detail-rerun"]').text()).toContain('Confirm re-run?')
    expect(wrapper.find('[data-testid="run-detail-rerun"]').text()).toContain('non-idempotent')

    await btn.trigger('click')
    await flushPromises()
    await nextTick()

    expect(api.POST).toHaveBeenCalledWith('/api/v1/runs/{run_id}/rerun', {
      params: { path: { run_id: 'test-run-id' } },
    })
    expect(routerPush).toHaveBeenCalledWith('/runs/new-run-id')
    wrapper.unmount()
  })

  it('requires a confirm click when the graph is empty or unreadable', async () => {
    // Default mock returns an empty node list → confirm-required path.
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const btn = wrapper.find('[data-testid="run-rerun"]')
    await btn.trigger('click')
    await nextTick()
    expect(api.POST).not.toHaveBeenCalledWith('/api/v1/runs/{run_id}/rerun', expect.anything())

    await btn.trigger('click')
    await flushPromises()
    await nextTick()
    expect(api.POST).toHaveBeenCalledWith('/api/v1/runs/{run_id}/rerun', {
      params: { path: { run_id: 'test-run-id' } },
    })
    wrapper.unmount()
  })

  it('shows an error and does not navigate when the rerun request fails', async () => {
    mockGraphNodes = [{ idempotent: true }]
    mockRerunError = { detail: 'Run is not in a terminal status' }
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="run-rerun"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="run-detail-rerun"]').text()).toContain('Failed to re-run:')
    expect(routerPush).not.toHaveBeenCalledWith('/runs/new-run-id')
    wrapper.unmount()
  })
})
