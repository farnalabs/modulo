import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: 'test-run-id' } }),
  useRouter: () => ({ push: vi.fn() }),
  createRouter: vi.fn(() => ({
    install: vi.fn(),
    push: vi.fn(),
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
    isReady: vi.fn(() => Promise.resolve(true)),
  })),
  createWebHistory: vi.fn(() => ({})),
}))

const runResponse = {
  run_id: 'test-run-id',
  pipeline_id: 'test-pipeline',
  snapshot_id: 'snapshot-1',
  status: 'failed',
  claim_count: 2,
  started_at: '2026-10-21T10:00:00Z',
  dispatched_at: '2026-10-21T10:55:00Z',
  completed_at: '2026-10-21T11:50:00Z',
  total_cost_usd: null,
  node_token_usage: { 'node-a': { input_tokens: 1, output_tokens: 1 } },
  trace_id: null,
}

const ioResponse = {
  outputs_json: { 'node-a': { input: {}, output: {} } },
  node_telemetry: {
    'node-a': {
      attempt_key: 'run:test-run-id:node:node-a:2',
      wall_clock_time_ms: 3300000,
    },
  },
}

function baseGet(url: string): Promise<{ data?: unknown; error?: unknown }> {
  if (url === '/api/v1/runs/{run_id}') return Promise.resolve({ data: runResponse, error: undefined })
  if (url === '/api/v1/runs/{run_id}/io') return Promise.resolve({ data: ioResponse, error: undefined })
  if (url === '/api/v1/pipelines/{pipeline_id}') {
    return Promise.resolve({
      data: { retry_policy: { on: ['timeout'], max_retries: 2 }, node_timeout_seconds: 3300 },
      error: undefined,
    })
  }
  if (url === '/api/v1/pipelines/{pipeline_id}/snapshots/{snapshot_id}') {
    return Promise.resolve({
      data: { graph_json: { nodes: [{ id: 'node-a', timeout_seconds: 3300 }], edges: [] } },
      error: undefined,
    })
  }
  if (url === '/api/v1/pipelines/{pipeline_id}/graph') {
    return Promise.resolve({
      data: { nodes: [{ id: 'node-a', timeout_seconds: 3300 }], edges: [] },
      error: undefined,
    })
  }
  return Promise.resolve({ data: null, error: undefined })
}

vi.mock('../lib/api/client', () => ({
  api: { GET: vi.fn(baseGet) },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import RunDetailView from '../views/RunDetailView.vue'
import { api } from '../lib/api/client'

const getMock = api.GET as unknown as {
  mockImplementation: (fn: (url: string) => Promise<{ data?: unknown; error?: unknown }>) => void
}

function mountView() {
  return mount(RunDetailView, {
    global: {
      plugins: [createPinia()],
    },
  })
}

async function settle(): Promise<void> {
  await flushPromises()
  await flushPromises()
  await flushPromises()
}

describe('RunDetailView worst-case node time (FAR-2072)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    getMock.mockImplementation(baseGet)
    vi.spyOn(console, 'warn').mockImplementation(() => {})
  })

  it('derives the attempt count and ceiling from the pipeline retry policy', async () => {
    const wrapper = mountView()
    await settle()

    const panel = wrapper.find('[data-testid="run-detail-worst-case"]')
    expect(panel.exists()).toBe(true)

    const nodes = wrapper.findAll('[data-testid="run-detail-worst-case-node"]')
    expect(nodes).toHaveLength(1)
    expect(nodes[0].text()).toContain('2 attempts so far')
    // Timeout 55m x (1 + max_retries 2) = 2h45m.
    expect(nodes[0].text()).toContain('ceiling: 3 attempts × 55m = 2h45m')
    expect(panel.text()).toContain('Queue and dispatch time sit outside this calculation.')

    const attempts = wrapper.findAll('[data-testid="run-detail-worst-case-attempt"]')
    expect(attempts).toHaveLength(2)
    expect(attempts[0].text()).toBe('attempt 1: 55m')
    expect(attempts[1].text()).toBe('attempt 2: 55m')

    wrapper.unmount()
  })

  it('renders the ceiling as unavailable when the pipeline read fails', async () => {
    getMock.mockImplementation((url) => {
      if (url === '/api/v1/pipelines/{pipeline_id}') return Promise.reject(new Error('pipeline read failed'))
      return baseGet(url)
    })
    const wrapper = mountView()
    await settle()

    const nodes = wrapper.findAll('[data-testid="run-detail-worst-case-node"]')
    expect(nodes).toHaveLength(1)
    expect(nodes[0].find('[data-testid="run-detail-worst-case-unavailable"]').exists()).toBe(true)
    expect(nodes[0].find('[data-testid="run-detail-worst-case-ceiling"]').exists()).toBe(false)

    wrapper.unmount()
  })

  it('treats a malformed retry policy as a known no-retry policy', async () => {
    getMock.mockImplementation((url) => {
      if (url === '/api/v1/pipelines/{pipeline_id}') {
        return Promise.resolve({ data: { retry_policy: ['bad'], node_timeout_seconds: '3300' }, error: undefined })
      }
      return baseGet(url)
    })
    const wrapper = mountView()
    await settle()

    const nodes = wrapper.findAll('[data-testid="run-detail-worst-case-node"]')
    expect(nodes).toHaveLength(1)
    expect(nodes[0].text()).toContain('ceiling: 1 attempt × 55m = 55m')

    wrapper.unmount()
  })

  it('ignores malformed graph nodes when building the timeout overrides', async () => {
    getMock.mockImplementation((url) => {
      if (url === '/api/v1/pipelines/{pipeline_id}/snapshots/{snapshot_id}') {
        return Promise.resolve({
          data: {
            graph_json: {
              nodes: [
                null,
                'not-a-node',
                { id: 7, timeout_seconds: 3300 },
                { id: 'node-a', timeout_seconds: -5 },
              ],
              edges: [],
            },
          },
          error: undefined,
        })
      }
      return baseGet(url)
    })
    const wrapper = mountView()
    await settle()

    const nodes = wrapper.findAll('[data-testid="run-detail-worst-case-node"]')
    expect(nodes).toHaveLength(1)
    // No usable override, so the 55m pipeline default applies.
    expect(nodes[0].text()).toContain('ceiling: 3 attempts × 55m = 2h45m')

    wrapper.unmount()
  })

  it('falls back to the live pipeline graph when the snapshot has no graph', async () => {
    getMock.mockImplementation((url) => {
      if (url === '/api/v1/pipelines/{pipeline_id}/snapshots/{snapshot_id}') {
        return Promise.resolve({ data: { graph_json: null }, error: undefined })
      }
      if (url === '/api/v1/pipelines/{pipeline_id}/graph') {
        return Promise.resolve({ data: { nodes: [{ id: 'node-a', timeout_seconds: 600 }], edges: [] } })
      }
      return baseGet(url)
    })
    const wrapper = mountView()
    await settle()

    const nodes = wrapper.findAll('[data-testid="run-detail-worst-case-node"]')
    expect(nodes).toHaveLength(1)
    expect(nodes[0].text()).toContain('ceiling: 3 attempts × 10m = 30m')

    wrapper.unmount()
  })

  it('keeps the pipeline default timeout when the snapshot and graph reads fail', async () => {
    getMock.mockImplementation((url) => {
      if (url === '/api/v1/pipelines/{pipeline_id}/snapshots/{snapshot_id}') {
        return Promise.reject(new Error('snapshot read failed'))
      }
      if (url === '/api/v1/pipelines/{pipeline_id}/graph') {
        return Promise.reject(new Error('graph read failed'))
      }
      return baseGet(url)
    })
    const wrapper = mountView()
    await settle()

    const nodes = wrapper.findAll('[data-testid="run-detail-worst-case-node"]')
    expect(nodes).toHaveLength(1)
    expect(nodes[0].text()).toContain('ceiling: 3 attempts × 55m = 2h45m')

    wrapper.unmount()
  })

  it('falls back to run-level claims when telemetry has no attempt key', async () => {
    getMock.mockImplementation((url) => {
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({
          data: { outputs_json: { 'node-a': { input: {}, output: {} } }, node_telemetry: {} },
          error: undefined,
        })
      }
      return baseGet(url)
    })
    const wrapper = mountView()
    await settle()

    const nodes = wrapper.findAll('[data-testid="run-detail-worst-case-node"]')
    expect(nodes).toHaveLength(1)
    expect(nodes[0].text()).toContain('2 attempts so far')
    expect(wrapper.findAll('[data-testid="run-detail-worst-case-attempt"]')).toHaveLength(2)

    wrapper.unmount()
  })

  it('shows a running attempt without a snapshot and leaves unobservable intervals unrecorded', async () => {
    getMock.mockImplementation((url) => {
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({
          data: {
            ...runResponse,
            status: 'running',
            snapshot_id: null,
            dispatched_at: null,
            completed_at: null,
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({
          data: { outputs_json: { 'node-a': { input: {}, output: {} } }, node_telemetry: {} },
          error: undefined,
        })
      }
      if (url === '/api/v1/pipelines/{pipeline_id}/graph') {
        return Promise.resolve({ data: null, error: undefined })
      }
      return baseGet(url)
    })
    const wrapper = mountView()
    await settle()

    expect(wrapper.find('[data-testid="run-detail-worst-case"]').exists()).toBe(true)
    const nodes = wrapper.findAll('[data-testid="run-detail-worst-case-node"]')
    expect(nodes).toHaveLength(1)
    expect(nodes[0].text()).toContain('2 attempts so far')
    expect(nodes[0].text()).toContain('not individually recorded')

    wrapper.unmount()
  })

  it('does not show the worst-case panel for a completed run', async () => {
    getMock.mockImplementation((url) => {
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({ data: { ...runResponse, status: 'complete' }, error: undefined })
      }
      return baseGet(url)
    })
    const wrapper = mountView()
    await settle()

    expect(wrapper.find('[data-testid="run-detail-worst-case"]').exists()).toBe(false)

    wrapper.unmount()
  })

  it('treats a null pipeline payload as an unreadable policy', async () => {
    getMock.mockImplementation((url) => {
      if (url === '/api/v1/pipelines/{pipeline_id}') {
        return Promise.resolve({ data: null, error: undefined })
      }
      return baseGet(url)
    })
    const wrapper = mountView()
    await settle()

    const nodes = wrapper.findAll('[data-testid="run-detail-worst-case-node"]')
    expect(nodes).toHaveLength(1)
    expect(nodes[0].find('[data-testid="run-detail-worst-case-unavailable"]').exists()).toBe(true)

    wrapper.unmount()
  })

  it('coerces a zero or unparseable claim count to a single attempt', async () => {
    for (const claimCount of [0, 'not-a-number']) {
      getMock.mockImplementation((url) => {
        if (url === '/api/v1/runs/{run_id}') {
          return Promise.resolve({ data: { ...runResponse, claim_count: claimCount }, error: undefined })
        }
        if (url === '/api/v1/runs/{run_id}/io') {
          return Promise.resolve({
            data: { outputs_json: { 'node-a': { input: {}, output: {} } }, node_telemetry: {} },
            error: undefined,
          })
        }
        return baseGet(url)
      })
      const wrapper = mountView()
      await settle()

      const nodes = wrapper.findAll('[data-testid="run-detail-worst-case-node"]')
      expect(nodes).toHaveLength(1)
      expect(nodes[0].text()).toContain('1 attempt so far')
      expect(wrapper.findAll('[data-testid="run-detail-worst-case-attempt"]')).toHaveLength(1)

      wrapper.unmount()
    }
  })
})
