import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: 'test-run-id' } }),
  useRouter: () => ({ push: vi.fn() }),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({
          data: {
            run_id: 'test-run-id',
            run_number: 'test-run-id',
            pipeline_id: 'test-pipeline',
            status: 'complete',
            total_cost_usd: 1.23,
            token_consumption: null,
            node_token_usage: {},
            trace_id: null,
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({ data: { outputs_json: {} }, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import RunDetailView from '../views/RunDetailView.vue'

describe('RunDetailShare', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders Share Summary button when run loads', async () => {
    const wrapper = mount(RunDetailView, {
      global: {
        mocks: { $t: (key: string) => key.split('.').pop() ?? key },
      },
    })
    await nextTick()
    await new Promise(r => setTimeout(r, 0))
    const btn = wrapper.find('[data-testid="run-detail-share-summary"]')
    expect(btn.exists()).toBe(true)
    expect(btn.text()).toBe('Share Summary')
  })

  it('copies share summary to clipboard on click', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })

    const wrapper = mount(RunDetailView, {
      global: {
        mocks: { $t: (key: string) => key.split('.').pop() ?? key },
      },
    })
    await nextTick()
    await new Promise(r => setTimeout(r, 0))

    await wrapper.find('[data-testid="run-detail-share-summary"]').trigger('click')

    expect(writeText).toHaveBeenCalledOnce()
    const text = writeText.mock.calls[0][0]
    expect(text).toContain('Run: #test-run-id')
    expect(text).toContain('Pipeline: #test')
    expect(text).toContain('Status: complete')
    expect(text).toContain('Nodes: 0/0')
    expect(text).toContain('Cost: $1.230000')
  })

  it('shows Copied! text after clicking share button', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })

    const wrapper = mount(RunDetailView, {
      global: {
        mocks: { $t: (key: string) => key.split('.').pop() ?? key },
      },
    })
    await nextTick()
    await new Promise(r => setTimeout(r, 0))

    await wrapper.find('[data-testid="run-detail-share-summary"]').trigger('click')
    expect(wrapper.find('[data-testid="run-detail-share-summary"]').text()).toBe('Copied!')
  })
})
