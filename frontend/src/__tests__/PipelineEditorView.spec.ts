import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('../composables/useApi', () => ({
  useApi: vi.fn(() => ({
    get: vi.fn().mockImplementation((url: string) => {
      if (url.includes('/lifecycle-maps')) return Promise.resolve([])
      if (url.includes('/pipeline-folders')) return Promise.resolve([])
      if (url.includes('/graph')) return Promise.resolve({ nodes: [], edges: [] })
      if (url.includes('/agents')) return Promise.resolve({ items: [] })
      if (url.includes('/connectors')) return Promise.resolve({ items: [] })
      if (url.includes('/model-backends')) return Promise.resolve({ items: [] })
      if (url.includes('/schemas')) return Promise.resolve({ items: [] })
      if (url.includes('/snapshots')) return Promise.resolve({ items: [] })
      return Promise.resolve({ items: [] })
    }),
    post: vi.fn().mockResolvedValue({}),
  })),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({ data: { items: [] }, error: undefined }),
    POST: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
    PATCH: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import PipelineEditorView from '../views/PipelineEditorView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/pipelines/:id/editor', name: 'pipeline-editor', component: PipelineEditorView },
  ],
})

describe('PipelineEditorView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mount(PipelineEditorView, {
      global: {
        plugins: [createPinia(), router],
        stubs: {
          VueFlow: { template: '<div><slot /></div>' },
          Background: true,
          Controls: true,
        },
      },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
  })
})
