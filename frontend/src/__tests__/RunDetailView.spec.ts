import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({
          data: {
            run_id: 'test-run-id',
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

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/runs/:id', name: 'run-detail', component: RunDetailView },
  ],
})

describe('RunDetailView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = mount(RunDetailView, {
      global: { plugins: [router] },
    })
    await nextTick()
    await new Promise(r => setTimeout(r, 0))
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Run Detail')
  })
})
