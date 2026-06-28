import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({ data: { items: [] }, error: undefined }),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import SchemaInferenceView from '../views/SchemaInferenceView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/schemas/infer', name: 'schema-infer', component: SchemaInferenceView },
    { path: '/library', name: 'library', component: { template: '<div/>' } },
  ],
})

describe('SchemaInferenceView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    router.push('/schemas/infer')
    await router.isReady()
    const wrapper = mount(SchemaInferenceView, {
      global: {
        plugins: [router],
        stubs: { RouterLink: true },
      },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Schema Inference')
  })
})
