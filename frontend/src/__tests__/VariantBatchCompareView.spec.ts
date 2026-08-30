import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, reactive } from 'vue'

const routerMocks = vi.hoisted(() => ({
  push: vi.fn().mockResolvedValue(undefined),
  replace: vi.fn().mockResolvedValue(undefined),
}))

const routeHolder = vi.hoisted(() => ({
  route: null as unknown,
}))

const makeRoute = (batchId: string) => ({
  get path() {
    return `/variants/compare/${batchId}`
  },
  get fullPath() {
    return `/variants/compare/${batchId}`
  },
  params: { batchId },
  query: {},
  hash: '',
  matched: [],
  name: 'variant-batch-compare',
  redirectedFrom: undefined,
  meta: {},
})

vi.mock('vue-router', () => ({
  useRoute: vi.fn(() => routeHolder.route),
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
    isReady: vi.fn(() => Promise.resolve(true)),
  })),
}))

const batchMocks = vi.hoisted(() => ({
  fetchVariantBatch: vi.fn(),
  fetchVariantBatches: vi.fn(),
  softDeleteVariantBatch: vi.fn(),
  reFireVariantBatch: vi.fn(),
}))

vi.mock('../lib/api/variantBatches', () => ({
  fetchVariantBatch: batchMocks.fetchVariantBatch,
  fetchVariantBatches: batchMocks.fetchVariantBatches,
  softDeleteVariantBatch: batchMocks.softDeleteVariantBatch,
  reFireVariantBatch: batchMocks.reFireVariantBatch,
  TERMINAL_STATUSES: ['complete', 'failed', 'cancelled', 'eval_failed', 'stalled', 'budget_exceeded', 'router_no_match', 'cost_ceiling_exceeded', 'compensation_failed'],
  NON_TERMINAL_STATUSES: ['pending', 'running', 'awaiting_human', 'claimed', 'unknown'],
}))

import VariantBatchCompareView from '../views/VariantBatchCompareView.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'

const run = (overrides: Record<string, unknown> = {}) => ({
  run_id: 'r1',
  variant_name: 'opus',
  snapshot_label: 'v3',
  input_label: 'Opus',
  run_status: 'complete',
  pass_rate: 95,
  total_cost_usd: 1.23,
  total_tokens: 4500,
  eval_results: [{ eval_id: 'e1', node_id: 'node-a', passed: true, score: 0.95 }],
  node_outputs: { summary: 'x' },
  ...overrides,
})

const mockBatch = (overrides: Record<string, unknown> = {}) => ({
  batch_id: 'b1',
  name: 'Sonnet vs Opus',
  pipeline_id: 'p1',
  pipeline_name: 'Summarizer',
  status: 'complete',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  runs: [
    run({ run_id: 'r1', variant_name: 'opus' }),
    run({ run_id: 'r2', variant_name: 'sonnet', run_status: 'complete', pass_rate: 80, total_cost_usd: 0.5, total_tokens: 3000, eval_results: [], node_outputs: { summary: 'y' } }),
  ],
  ...overrides,
})

const mockComparisons = () => ({
  items: [
    { batch_id: 'b1', name: 'Sonnet vs Opus', pipeline_name: 'Summarizer', status: 'complete', run_count: 2, created_at: '2026-01-01T00:00:00Z' },
    { batch_id: 'b2', name: 'v3 vs v4', pipeline_name: 'Summarizer', status: 'running', run_count: 2, created_at: '2026-01-02T00:00:00Z' },
  ],
  total: 2,
})

describe('VariantBatchCompareView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    routeHolder.route = reactive(makeRoute('b1'))
    batchMocks.fetchVariantBatch.mockResolvedValue({ data: mockBatch(), error: undefined })
    batchMocks.fetchVariantBatches.mockResolvedValue({ data: mockComparisons(), error: undefined })
    batchMocks.softDeleteVariantBatch.mockResolvedValue({})
    batchMocks.reFireVariantBatch.mockResolvedValue({ data: mockBatch({ status: 'running' }), error: undefined })
  })

  it('renders the ranked table from the route batch JSON with fixed columns', async () => {
    const wrapper = mount(VariantBatchCompareView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()

    const text = wrapper.text()
    expect(text).toContain('Label')
    expect(text).toContain('Snapshot / Input')
    expect(text).toContain('Status')
    expect(text).toContain('Pass Rate')
    expect(text).toContain('Cost')

    // both variants render
    expect(text).toContain('opus')
    expect(text).toContain('sonnet')
    // ranked: opus (95%) first
    const rows = wrapper.findAll('tbody tr')
    expect(rows[0].text()).toContain('opus')
    expect(rows[1].text()).toContain('sonnet')

    expect(batchMocks.fetchVariantBatch).toHaveBeenCalledWith('b1')
  })

  it('maps per-variant status badges to the RUN_STATUS vocabulary', async () => {
    batchMocks.fetchVariantBatch.mockResolvedValue({
      data: mockBatch({
        runs: [
          run({ run_id: 'r1', variant_name: 'a', run_status: 'complete', pass_rate: 100 }),
          run({ run_id: 'r2', variant_name: 'b', run_status: 'failed', pass_rate: null, total_cost_usd: null, total_tokens: null, eval_results: [], node_outputs: null }),
          run({ run_id: 'r3', variant_name: 'c', run_status: 'running', pass_rate: null, total_cost_usd: null, total_tokens: null, eval_results: [], node_outputs: null }),
        ],
      }),
      error: undefined,
    })

    const wrapper = mount(VariantBatchCompareView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()

    const badges = wrapper.findAll('[data-testid="variant-batch-status-badge"]')
    expect(badges).toHaveLength(3)
    expect(badges[0].text()).toContain('Complete')
    expect(badges[1].text()).toContain('Failed')
    expect(badges[2].text()).toContain('Running')
  })

  it('renders partial-results notice when some variants fail but others complete', async () => {
    batchMocks.fetchVariantBatch.mockResolvedValue({
      data: mockBatch({
        status: 'partial',
        runs: [
          run({ run_id: 'r1', variant_name: 'a', run_status: 'complete', pass_rate: 90 }),
          run({ run_id: 'r2', variant_name: 'b', run_status: 'failed', pass_rate: null, total_cost_usd: null, total_tokens: null, eval_results: [], node_outputs: null }),
        ],
      }),
      error: undefined,
    })

    const wrapper = mount(VariantBatchCompareView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="variant-batch-partial"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Some variants failed')
  })

  it('shows the server-computed completion signal', async () => {
    batchMocks.fetchVariantBatch.mockResolvedValue({ data: mockBatch({ status: 'complete' }), error: undefined })
    const wrapper = mount(VariantBatchCompareView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="variant-batch-status"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Complete')
  })

  it('expands a row to reveal deep output/eval detail', async () => {
    const wrapper = mount(VariantBatchCompareView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="variant-batch-detail-r1"]').exists()).toBe(false)

    await wrapper.find('[data-testid="variant-batch-expand-r1"]').trigger('click')
    await nextTick()

    expect(wrapper.find('[data-testid="variant-batch-detail-r1"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Evals')
    expect(wrapper.find('[data-testid="variant-batch-run-link-r1"]').exists()).toBe(true)
  })

  it('re-fires the batch from the frozen batch', async () => {
    const wrapper = mount(VariantBatchCompareView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="variant-batch-refire"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(batchMocks.reFireVariantBatch).toHaveBeenCalledWith('b1')
    expect(wrapper.text()).toContain('Running')
  })

  it('navigates the route to the new batch id after a successful re-fire', async () => {
    batchMocks.reFireVariantBatch.mockResolvedValue({
      data: mockBatch({ batch_id: 'b9', name: 'Rebuilt comparison' }),
      error: undefined,
    })

    const wrapper = mount(VariantBatchCompareView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="variant-batch-refire"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(routerMocks.replace).toHaveBeenCalledWith('/variants/compare/b9')
  })

  it('re-enables the Re-fire button when the route moves on before the re-fire response returns', async () => {
    // Control the re-fire request so we can move the route mid-flight.
    let resolveReFire: (value: unknown) => void = () => {}
    batchMocks.reFireVariantBatch.mockImplementationOnce(
      () => new Promise((res) => { resolveReFire = res }),
    )

    const wrapper = mount(VariantBatchCompareView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()

    const refire = () => wrapper.find('[data-testid="variant-batch-refire"]')

    // Fire the re-fire; the request is in flight and the button is disabled.
    await refire().trigger('click')
    await flushPromises()
    await nextTick()
    expect(refire().attributes('disabled')).toBeDefined()

    // The route moves on to a different batch (b2) before the response returns.
    ;(routeHolder.route as { params: { batchId: string } }).params.batchId = 'b2'
    await nextTick()

    // The response arrives for a brand-new batch id (b9). Because the route has
    // already moved on, the id-guard short-circuits and the new batch is NOT
    // adopted. The real bug (pre-fix) was that the finally-guard
    // `if (thisId === batchId.value) refiring.value = false` then skipped the
    // reset, leaving the CURRENT batch's Re-fire button permanently disabled.
    // The fix makes the finally reset unconditional, so the button re-enables.
    resolveReFire({ data: mockBatch({ batch_id: 'b9', name: 'Rebuilt comparison' }), error: undefined })
    await flushPromises()
    await nextTick()

    expect(refire().attributes('disabled')).toBeUndefined()
  })

  it('drops a stale loadBatch response that resolves after the route moved on', async () => {
    let resolveSlow: (value: unknown) => void = () => {}
    batchMocks.fetchVariantBatch.mockImplementationOnce(() => new Promise((res) => { resolveSlow = res }))
    batchMocks.fetchVariantBatch.mockResolvedValue({ data: mockBatch({ name: 'latest' }), error: undefined })

    const wrapper = mount(VariantBatchCompareView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()

    // Route moves on to a different batch while the first request is still in flight.
    ;(routeHolder.route as { params: { batchId: string } }).params.batchId = 'b2'
    resolveSlow({ data: mockBatch({ name: 'stale' }), error: undefined })
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).not.toContain('stale')
  })

  it('keeps the loading flag set when a stale response resolves after the route moved on', async () => {
    let resolveStale: (value: unknown) => void = () => {}
    batchMocks.fetchVariantBatch.mockImplementationOnce(() => new Promise((res) => { resolveStale = res }))
    // The new batch's request stays in flight (never settles), so the loading flag must remain set.
    batchMocks.fetchVariantBatch.mockImplementationOnce(() => new Promise<never>(() => {}))

    const wrapper = mount(VariantBatchCompareView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()

    // Route moves on to b2 while b1's request is still in flight.
    ;(routeHolder.route as { params: { batchId: string } }).params.batchId = 'b2'
    await nextTick()

    // The stale b1 response resolves — it must NOT clear the loading flag for the current batch.
    resolveStale({ data: mockBatch({ name: 'stale' }), error: undefined })
    await flushPromises()
    await nextTick()

    expect(wrapper.findComponent(LoadingSpinner).exists()).toBe(true)
  })

  it('lists My comparisons with soft-delete', async () => {
    const wrapper = mount(VariantBatchCompareView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('My comparisons')
    expect(wrapper.find('[data-testid="variant-batch-link-b1"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="variant-batch-link-b2"]').exists()).toBe(true)

    // soft-delete b2
    await wrapper.find('[data-testid="variant-batch-delete-b2"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(batchMocks.softDeleteVariantBatch).toHaveBeenCalledWith('b2')
    // list reloaded after delete
    expect(batchMocks.fetchVariantBatches).toHaveBeenCalledTimes(2)
  })

  it('renders a partial batch status with its translated label, not raw lowercase', async () => {
    batchMocks.fetchVariantBatches.mockResolvedValue({
      data: {
        items: [
          { batch_id: 'b1', name: 'Sonnet vs Opus', pipeline_name: 'Summarizer', status: 'complete', run_count: 2, created_at: '2026-01-01T00:00:00Z' },
          { batch_id: 'b3', name: 'partial batch', pipeline_name: 'Summarizer', status: 'partial', run_count: 1, created_at: '2026-01-02T00:00:00Z' },
        ],
        total: 2,
      },
      error: undefined,
    })

    const wrapper = mount(VariantBatchCompareView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Partial — some variants incomplete')
    expect(wrapper.text()).not.toContain('>partial<')
  })
})
