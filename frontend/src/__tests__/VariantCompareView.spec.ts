import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('vue-i18n', () => ({
  useI18n: () => ({ t: (key: string) => key }),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') return Promise.resolve({ data: [], error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    }),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import { api } from '../lib/api/client'
import VariantCompareView from '../views/VariantCompareView.vue'

const groupWithVariants = {
  id: 'g1',
  name: 'Group 1',
  pipeline_id: 'p1',
  variants: [
    { name: 'var-a', weight: 0.5 },
    { name: 'var-b', weight: 0.5 },
  ],
  run_count: 0,
  selection_strategy: 'weighted',
}

describe('VariantCompareView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await nextTick()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Variants')
  })

  it('renders the pure agent return in the diff viewers with no telemetry keys', async () => {
    vi.useFakeTimers()

    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') {
        return Promise.resolve({ data: [groupWithVariants], error: undefined })
      }
      if (url === '/api/v1/variant-groups/{group_id}') {
        return Promise.resolve({ data: groupWithVariants, error: undefined })
      }
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({
          data: { run_id: 'r1', status: 'complete', total_cost_usd: 0, token_consumption: null },
          error: undefined,
        })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({
          data: { outputs_json: { 'node-a': { summary: 'x', result: 'ok' } } },
          error: undefined,
        })
      }
      if (url === '/api/v1/runs/{run_id}/evals') {
        return Promise.resolve({ data: { items: [] }, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    let runCalls = 0
    vi.mocked(api.POST as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups/{group_id}/run') {
        runCalls += 1
        const variantName = runCalls === 1 ? 'var-a' : 'var-b'
        return Promise.resolve({ data: { run_id: `r${runCalls}`, variant_name: variantName }, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await nextTick()
    await nextTick()
    await vi.advanceTimersByTimeAsync(0)

    const runBtn = wrapper.find('[data-testid="variant-compare-run-variants"]')
    expect(runBtn.exists()).toBe(true)

    await runBtn.trigger('click')
    await vi.advanceTimersByTimeAsync(2000)
    await nextTick()

    await runBtn.trigger('click')
    await vi.advanceTimersByTimeAsync(2000)
    await nextTick()
    await nextTick()

    const viewers = wrapper.findAll('[data-testid="json-viewer"]')
    expect(viewers.length).toBeGreaterThanOrEqual(2)
    const text = viewers.map(v => v.text()).join(' ')
    expect(text).toContain('summary')
    expect(text).toContain('x')
    expect(text).toContain('result')
    expect(text).toContain('ok')
    expect(text).not.toContain('agent_stdout')
    expect(text).not.toContain('agent_stderr')
    expect(text).not.toContain('exit_code')
    expect(text).not.toContain('stall_reason')
    vi.useRealTimers()
  })

  it('renders per-node pass/fail/partial eval badges and scores side by side', async () => {
    vi.useFakeTimers()

    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') {
        return Promise.resolve({ data: [groupWithVariants], error: undefined })
      }
      if (url === '/api/v1/variant-groups/{group_id}') {
        return Promise.resolve({ data: groupWithVariants, error: undefined })
      }
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({
          data: { run_id: 'r1', status: 'complete', total_cost_usd: 0, token_consumption: null },
          error: undefined,
        })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({
          data: {
            outputs_json: {
              'node-a': { ok: true },
              'node-b': { ok: false },
              'node-c': { ok: 'mixed' },
            },
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/runs/{run_id}/evals') {
        return Promise.resolve({
          data: {
            items: [
              { eval_id: 'e1', node_id: 'node-a', passed: true, score: 0.95 },
              { eval_id: 'e2', node_id: 'node-b', passed: false, score: 0.3 },
              { eval_id: 'e3', node_id: 'node-c', passed: true, score: 0.8 },
              { eval_id: 'e4', node_id: 'node-c', passed: false, score: 0.2 },
            ],
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    vi.mocked(api.POST as unknown as (url: string) => Promise<unknown>).mockResolvedValue({
      data: { run_id: 'r1', variant_name: 'var-a' },
      error: undefined,
    })

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await nextTick()
    await nextTick()
    await vi.advanceTimersByTimeAsync(0)

    const runBtn = wrapper.find('[data-testid="variant-compare-run-variants"]')
    await runBtn.trigger('click')
    await vi.advanceTimersByTimeAsync(2000)
    await nextTick()

    const text = wrapper.text()
    expect(text).toContain('views.variantCompare.statusPass')
    expect(text).toContain('views.variantCompare.statusFail')
    expect(text).toContain('views.variantCompare.statusPartial')
    expect(text).toContain('0.95')
    expect(text).toContain('0.30')
    expect(text).toContain('0.80')
    expect(text).toContain('0.20')
    vi.useRealTimers()
  })

  it('shows per-variant cost and token totals in the summary footer', async () => {
    vi.useFakeTimers()

    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') {
        return Promise.resolve({ data: [groupWithVariants], error: undefined })
      }
      if (url === '/api/v1/variant-groups/{group_id}') {
        return Promise.resolve({ data: groupWithVariants, error: undefined })
      }
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({
          data: {
            run_id: 'r1',
            status: 'complete',
            total_cost_usd: 1.23,
            token_consumption: { total_tokens: 4500 },
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({ data: { outputs_json: { 'node-a': { summary: 'x' } } }, error: undefined })
      }
      if (url === '/api/v1/runs/{run_id}/evals') {
        return Promise.resolve({
          data: { items: [{ eval_id: 'e1', node_id: 'node-a', passed: true, score: 0.99 }] },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    vi.mocked(api.POST as unknown as (url: string) => Promise<unknown>).mockResolvedValue({
      data: { run_id: 'r1', variant_name: 'var-a' },
      error: undefined,
    })

    const tWithParams = (key: string, params?: Record<string, unknown>) =>
      params && Object.keys(params).length > 0 ? `${key}:${JSON.stringify(params)}` : key

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: tWithParams },
      },
    })
    await nextTick()
    await nextTick()
    await vi.advanceTimersByTimeAsync(0)

    const runBtn = wrapper.find('[data-testid="variant-compare-run-variants"]')
    await runBtn.trigger('click')
    await vi.advanceTimersByTimeAsync(2000)
    await nextTick()

    const text = wrapper.text()
    // interpolated $t output carries the token total; pass rate renders raw as a percentage
    expect(text).toContain('"count":"4,500"')
    expect(text).toContain('100%')
    expect(text).toContain('"cost":"$1.230000"')
    // var-b has no run data — its pass-rate cell shows an em dash
    expect(text).toContain('—')
    vi.useRealTimers()
  })
})
