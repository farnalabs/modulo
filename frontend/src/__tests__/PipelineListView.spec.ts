import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'

const mockResponses: Record<string, unknown> = {
  default: { items: [], total: 0, page: 1, page_size: 100 },
}

vi.mock('../composables/useApi', () => ({
  useApi: vi.fn(() => ({
    get: vi.fn((url: string) => Promise.resolve(mockResponses[url] ?? mockResponses.default)),
  })),
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
  mockResponses['/api/v1/pipelines?page_size=100'] = { items: [], total: 0, page: 1, page_size: 100 }
})

function flushPromises() {
  return new Promise(resolve => setTimeout(resolve, 0))
}

describe('PipelineListView', () => {
  it('renders without crashing', async () => {
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: {
        plugins: [router],
        stubs: { ErrorAlert: true },
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
        stubs: { ErrorAlert: true },
      },
    })
    expect(wrapper.find('[data-testid="pipeline-list-search"]').exists()).toBe(true)
  })

  it('renders empty state when no pipelines exist', async () => {
    await router.push('/pipelines')
    await router.isReady()
    const wrapper = mount(PipelineListView, {
      global: {
        plugins: [router],
        stubs: { ErrorAlert: true },
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
        stubs: { ErrorAlert: true },
      },
    })
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Test Pipeline')
  })

  it('renders pagination controls', async () => {
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
        stubs: { ErrorAlert: true },
      },
    })
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-list-prev-page"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-list-next-page"]').exists()).toBe(true)
  })
})
