import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'

vi.mock('../composables/useApi', () => ({
  useApi: vi.fn(() => ({
    get: vi.fn().mockResolvedValue({
      id: 'test-id',
      primitive_type: 'pipeline_template',
      name: 'Test Template',
      description: 'A test template',
      author: 'Test Author',
      version: '1.0.0',
      tags: [],
      visibility: 'org',
      content_json: { agents: [] },
    }),
    post: vi.fn().mockResolvedValue({ id: 'new-id', name: 'Test Pipeline' }),
  })),
}))

vi.mock('../lib/api/client', () => ({
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
  },
}))

import LibraryPipelineWizard from '../views/LibraryPipelineWizard.vue'
import { api } from '../lib/api/client'

const PRIMITIVE = {
  id: 'test-id',
  primitive_type: 'pipeline_template',
  name: 'Test Template',
  description: 'A test template',
  author: 'Test Author',
  version: '1.0.0',
  tags: [],
  visibility: 'org',
  content_json: { agents: [] },
}

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/library/:id/create-pipeline', name: 'library-pipeline-wizard', component: LibraryPipelineWizard },
    { path: '/library', name: 'library', component: { template: '<div/>' } },
  ],
})

describe('LibraryPipelineWizard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.GET).mockResolvedValue({ data: PRIMITIVE, error: undefined })
    vi.mocked(api.POST).mockResolvedValue({ data: { id: 'new-id', name: 'Test Pipeline' }, error: undefined } as never)
  })

  it('renders without crashing', async () => {
    router.push('/library/test-id/create-pipeline')
    await router.isReady()
    const wrapper = mount(LibraryPipelineWizard, {
      global: {
        plugins: [router],
        stubs: { OwnershipPicker: true },
      },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Create Pipeline from Template')
  })

  it('renders the shared ErrorAlert with a working Retry when pipeline creation fails (FAR-1655)', async () => {
    // createPipeline only surfaces createError when the POST throws, so the
    // first call rejects; Retry (which re-invokes createPipeline) succeeds.
    let postCalls = 0
    vi.mocked(api.POST).mockImplementation(async () => {
      postCalls += 1
      if (postCalls === 1) throw new Error('create exploded')
      return { data: { id: 'new-id', name: 'Test Pipeline' }, error: undefined } as never
    })

    router.push('/library/test-id/create-pipeline')
    await router.isReady()
    const wrapper = mount(LibraryPipelineWizard, {
      global: {
        plugins: [router],
        stubs: { OwnershipPicker: true, VueFlow: true, Background: true },
      },
    })
    await flushPromises()

    await wrapper.find('[data-testid="library-wizard-create"]').trigger('click')
    await flushPromises()

    // The bespoke createError <div> is gone; the shared ErrorAlert renders it.
    const alert = wrapper.find('[data-testid="error-alert-retry"]')
    expect(alert.exists()).toBe(true)
    expect(wrapper.text()).toContain('create exploded')

    await alert.trigger('click')
    await flushPromises()

    expect(postCalls).toBe(2)
    expect(wrapper.find('[data-testid="error-alert-retry"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Test Pipeline')
  })
})
