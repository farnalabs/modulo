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
})
