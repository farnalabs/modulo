import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

const mockResponses: Record<string, unknown> = {
  default: { items: [], total: 0, page: 1, page_size: 100 },
}

vi.mock('../lib/api/client', () => {
  const mockGet = vi.fn((url: string, options?: { params?: { query?: { page_size?: number } } }) => {
    if (url === '/api/v1/pipeline-folders') {
      return Promise.resolve({ data: mockResponses['/api/v1/pipeline-folders'] ?? [], error: undefined })
    }
    if (url === '/api/v1/pipelines') {
      const pageSize = options?.params?.query?.page_size ?? 100
      return Promise.resolve({
        data: mockResponses[`/api/v1/pipelines?page_size=${pageSize}`] ?? mockResponses.default,
        error: undefined,
      })
    }
    return Promise.resolve({ data: mockResponses.default, error: undefined })
  })
  return {
    api: {
      GET: mockGet,
      PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
      POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
      PATCH: vi.fn().mockResolvedValue({ data: null, error: undefined }),
      DELETE: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    },
    getAccessToken: vi.fn().mockReturnValue('mock-token'),
  }
})

vi.mock('../composables/useApi', () => ({
  useApi: () => ({
    get: vi.fn((url: string) => Promise.resolve(mockResponses[url] ?? [])),
    post: vi.fn(),
    patch: vi.fn(),
  }),
}))

import PipelineListView from '../views/PipelineListView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/pipelines', name: 'pipeline-list', component: PipelineListView },
    { path: '/pipelines/:id/editor', name: 'pipeline-editor', component: { template: '<div>editor</div>' } },
    { path: '/library', name: 'library', component: { template: '<div>library</div>' } },
  ],
})

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  mockResponses['/api/v1/pipelines?page_size=100'] = { items: [], total: 0, page: 1, page_size: 100 }
})

describe('PipelineListView', () => {
  it('renders without crashing', async () => {
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: {
        plugins: [router],
        stubs: { ErrorAlert: true, FolderTree: true },
      },
    })
    expect(wrapper.exists()).toBe(true)
  })

  it('renders the search bar with correct testid', async () => {
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: {
        plugins: [router],
        stubs: { ErrorAlert: true, FolderTree: true },
      },
    })
    expect(wrapper.find('[data-testid="filter-bar-search"]').exists()).toBe(true)
  })

  it('renders empty state when no pipelines exist', async () => {
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: {
        plugins: [router],
        stubs: { ErrorAlert: true, FolderTree: true },
      },
    })
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('No pipelines yet')
  })

  it('renders pipelines when data is returned', async () => {
    mockResponses['/api/v1/pipelines?page_size=100'] = {
      items: [
        { id: 'p1', organisation_id: 'org1', name: 'Test Pipeline', description: 'A test', visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z' },
      ],
      total: 1,
      page: 1,
      page_size: 100,
    }
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: {
        plugins: [router],
        stubs: { ErrorAlert: true, FolderTree: true },
      },
    })
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Test Pipeline')
  })

  it('renders many pipelines on mount', async () => {
    const manyPipelines = Array.from({ length: 15 }, (_, i) => ({
      id: `p${i}`, organisation_id: 'org1', name: `Pipeline ${i}`, description: null, visibility: 'org', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z',
    }))
    mockResponses['/api/v1/pipelines?page_size=100'] = {
      items: manyPipelines,
      total: 15,
      page: 1,
      page_size: 100,
    }
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: {
        plugins: [router],
        stubs: { ErrorAlert: true, FolderTree: true },
      },
    })
    expect(wrapper.exists()).toBe(true)
  })
})
