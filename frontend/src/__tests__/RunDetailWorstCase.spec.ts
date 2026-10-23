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

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({
          data: {
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
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({
          data: {
            outputs_json: { 'node-a': { input: {}, output: {} } },
            node_telemetry: {
              'node-a': {
                attempt_key: 'run:test-run-id:node:node-a:2',
                wall_clock_time_ms: 3300000,
              },
            },
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/pipelines/{pipeline_id}') {
        return Promise.resolve({
          data: {
            retry_policy: { on: ['timeout'], max_retries: 2 },
            node_timeout_seconds: 3300,
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/pipelines/{pipeline_id}/snapshots/{snapshot_id}') {
        return Promise.resolve({
          data: {
            graph_json: { nodes: [{ id: 'node-a', timeout_seconds: 3300 }], edges: [] },
          },
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
    }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import RunDetailView from '../views/RunDetailView.vue'

const translations: Record<string, string> = {
  'views.RunDetailView.worst_case_title': 'Worst-case node time',
  'views.RunDetailView.worst_case_help': 'help',
  'views.RunDetailView.worst_case_queue_note': 'Queue and dispatch time sit outside this calculation.',
  'views.RunDetailView.worst_case_unavailable': 'Ceiling unavailable',
}

function t(key: string, params?: Record<string, unknown>): string {
  if (key === 'views.RunDetailView.worst_case_attempts_so_far') return `${params?.count} attempts so far`
  if (key === 'views.RunDetailView.worst_case_ceiling') {
    return `ceiling: ${params?.count} attempts × ${params?.timeout} = ${params?.total}`
  }
  if (key === 'views.RunDetailView.worst_case_attempt_row') {
    return `attempt ${params?.attempt}: ${params?.elapsed}`
  }
  if (key === 'views.RunDetailView.worst_case_attempt_missing') return 'not individually recorded'
  return translations[key] ?? (key.split('.').pop() ?? key)
}

describe('RunDetailView worst-case node time (FAR-2072)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('derives the attempt count and ceiling from the pipeline retry policy', async () => {
    const wrapper = mount(RunDetailView, {
      global: {
        plugins: [createPinia()],
        mocks: { $t: t },
      },
    })
    await flushPromises()
    await flushPromises()

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
  })
})
