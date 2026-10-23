import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { nextTick } from 'vue'

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

vi.mock('vue-router', () => ({
  useRoute: vi.fn(() => testRoute),
  useRouter: vi.fn(() => ({
    push: vi.fn().mockResolvedValue(undefined),
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
  })),
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

const { getMock, postMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
}))

vi.mock('../lib/api/client', () => ({
  api: { GET: getMock, POST: postMock },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

vi.mock('../lib/jwt', () => ({
  decodeJwtPayload: () => ({ org_role: 'operator' }),
}))

import RunDetailView from '../views/RunDetailView.vue'

function baseRun(overrides: Record<string, unknown> = {}) {
  return {
    run_id: 'test-run-id',
    pipeline_id: 'test-pipeline',
    status: 'complete',
    total_cost_usd: 1.23,
    node_token_usage: null,
    trace_id: null,
    ...overrides,
  }
}

async function mountWithRun(run: Record<string, unknown>) {
  getMock.mockImplementation((url: string) => {
    if (url === '/api/v1/runs/{run_id}') return Promise.resolve({ data: run, error: undefined })
    if (url === '/api/v1/runs/{run_id}/io') return Promise.resolve({ data: { outputs_json: null }, error: undefined })
    if (url === '/api/v1/runs/{run_id}/hitl/pending') return Promise.resolve({ data: { reviews: [] }, error: undefined })
    if (url === '/api/v1/pipelines/{pipeline_id}/graph') return Promise.resolve({ data: { nodes: [], edges: [] }, error: undefined })
    return Promise.resolve({ data: null, error: undefined })
  })
  const wrapper = mount(RunDetailView, { global: { plugins: [createPinia()] } })
  await flushPromises()
  await nextTick()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  postMock.mockResolvedValue({ data: { run_id: 'test-run-id', status: 'pending' }, error: undefined })
})

describe('RunDetailView — FAR-2054 seam coverage', () => {
  it('renders one row per seam statement with its state and provenance labels', async () => {
    const wrapper = await mountWithRun(
      baseRun({
        seam_coverage: [
          {
            seam: 'ingestion_edge',
            state: 'enforced',
            provenance: 'attested',
            reason: '2 ingestion-edge guardrail(s) evaluated at run creation',
          },
          {
            seam: 'node_output_edge',
            state: 'degraded',
            provenance: 'observed',
            reason: '1 node-output guardrail bound but the executor did not report',
          },
          {
            seam: 'runtime_loop',
            state: 'not_applicable',
            provenance: 'derived',
            reason: 'runtime does not expose a tool-event contract Modulo can verify',
          },
        ],
      }),
    )

    const section = wrapper.find('[data-testid="run-detail-seam-coverage"]')
    expect(section.exists()).toBe(true)
    expect(section.text()).toContain('Guardrail Coverage by Seam')
    expect(wrapper.findAll('[data-testid="run-detail-seam-coverage-row"]')).toHaveLength(3)
    const text = section.text()
    expect(text).toContain('Enforced')
    expect(text).toContain('Degraded')
    expect(text).toContain('Not applicable')
    expect(text).toContain('Attested')
    expect(text).toContain('Observed')
    expect(text).toContain('Derived')
  })

  it('renders reported provenance with its own badge class', async () => {
    const wrapper = await mountWithRun(
      baseRun({
        seam_coverage: [
          {
            seam: 'ingestion_edge',
            state: 'degraded',
            provenance: 'reported',
            reason: 'reported by an external source',
          },
        ],
      }),
    )

    const row = wrapper.find('[data-testid="run-detail-seam-coverage-row"]')
    expect(row.exists()).toBe(true)
    expect(row.text()).toContain('Reported')
    expect(row.findAll('span.inline-flex')[1].classes()).toContain('bg-warning/10')
  })

  it('falls back to the muted class for an unknown state or provenance', async () => {
    const wrapper = await mountWithRun(
      baseRun({
        seam_coverage: [
          {
            seam: 'ingestion_edge',
            state: 'unexpected_state',
            provenance: 'unexpected_provenance',
            reason: 'defensive',
          },
        ],
      }),
    )

    const rows = wrapper.findAll('[data-testid="run-detail-seam-coverage-row"]')
    expect(rows).toHaveLength(1)
    const badges = rows[0].findAll('span.inline-flex')
    expect(badges).toHaveLength(2)
    expect(badges[0].classes()).toContain('bg-muted')
    expect(badges[1].classes()).toContain('bg-muted')
  })

  it('renders no section when the run exposes no seam coverage', async () => {
    const wrapper = await mountWithRun(baseRun({ seam_coverage: null }))

    expect(wrapper.find('[data-testid="run-detail-seam-coverage"]').exists()).toBe(false)
  })
})
