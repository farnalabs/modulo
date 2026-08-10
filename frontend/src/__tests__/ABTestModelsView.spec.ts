import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') return Promise.resolve({ data: [] as any, error: undefined })
      return Promise.resolve({ data: { items: [] as any, total: 0, page: 1, page_size: 50 }, error: undefined })
    }),
    POST: vi.fn().mockResolvedValue({ data: { id: '1' }, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import { api } from '../lib/api/client'
import ABTestModelsView from '../views/ABTestModelsView.vue'

describe('ABTestModelsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(ABTestModelsView)
    await nextTick()
    await new Promise(r => setTimeout(r, 0))
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('A/B Test Models')
  })

  it('consumes a pure-return io fixture and leaks no telemetry into the rendered results', async () => {
    vi.useFakeTimers()

    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/pipelines') {
        return Promise.resolve({ data: { items: [{ id: 'p1', name: 'Pipe' }], total: 1, page: 1, page_size: 50 }, error: undefined })
      }
      if (url === '/api/v1/model-backends') {
        return Promise.resolve({
          data: {
            items: [
              { id: 'mb1', display_name: 'MB1', provider: 'openai' },
              { id: 'mb2', display_name: 'MB2', provider: 'anthropic' },
            ],
            total: 2,
            page: 1,
            page_size: 50,
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/variant-groups') {
        return Promise.resolve({ data: { items: [], total: 0, page: 1, page_size: 50 }, error: undefined })
      }
      if (url === '/api/v1/pipelines/{pipeline_id}/snapshots') {
        return Promise.resolve({ data: { items: [] }, error: undefined })
      }
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({
          data: { run_id: 'r1', status: 'complete', total_cost_usd: 1.2, token_consumption: { total_tokens: 100 } },
          error: undefined,
        })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({ data: { outputs_json: { 'node-a': { summary: 'x', result: 'ok' } } }, error: undefined })
      }
      if (url === '/api/v1/runs/{run_id}/evals') {
        return Promise.resolve({ data: { items: [] }, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    vi.mocked(api.POST as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') {
        return Promise.resolve({ data: { id: 'g1', name: 'Test Group', pipeline_id: 'p1', variants: [] }, error: undefined })
      }
      if (url === '/api/v1/variant-groups/{group_id}/run') {
        return Promise.resolve({ data: { run_id: 'r1', variant_name: 'Variant 1' }, error: undefined })
      }
      return Promise.resolve({ data: { id: '1' }, error: undefined })
    })

    const wrapper = mount(ABTestModelsView)
    await nextTick()
    await nextTick()
    await vi.advanceTimersByTimeAsync(0)

    await wrapper.find('[data-testid="ab-test-models-group-name"]').setValue('Test Group')
    await wrapper.find('[data-testid="ab-test-models-add-variant"]').trigger('click')
    await wrapper.find('[data-testid="ab-test-models-add-variant"]').trigger('click')
    await nextTick()

    await wrapper.find('[data-testid="ab-test-models-run-ab-test"]').trigger('click')
    await vi.advanceTimersByTimeAsync(2000)
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('Variant 1')
    const text = wrapper.text()
    expect(text).not.toContain('agent_stdout')
    expect(text).not.toContain('agent_stderr')
    expect(text).not.toContain('exit_code')
    vi.useRealTimers()
  })
})
