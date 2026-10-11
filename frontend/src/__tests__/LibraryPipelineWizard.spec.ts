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
    // A thrown transport/network failure still surfaces createError via the
    // catch path; the openapi-fetch { data, error } resolve path is covered
    // by the next test.
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

  it('surfaces an openapi-fetch { data: undefined, error } failure as the ErrorAlert with working Retry (FAR-1655)', async () => {
    // openapi-fetch resolves (never throws) on 4xx/5xx with { data, error }.
    // createPipeline must surface that error — the view previously only set
    // createError in the thrown-error catch, so HTTP failures silently no-oped.
    let postCalls = 0
    vi.mocked(api.POST).mockImplementation(async () => {
      postCalls += 1
      if (postCalls === 1) {
        return { data: undefined, error: { detail: 'create backend down' } } as never
      }
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

    // The resolved-error path renders the ErrorAlert with a retry button...
    const retry = wrapper.find('[data-testid="error-alert-retry"]')
    expect(retry.exists()).toBe(true)
    expect(wrapper.text()).toContain('create backend down')
    // ...and result.value is NOT set on failure (no success banner/link).
    expect(wrapper.find('[data-testid="library-wizard-view-pipeline"]').exists()).toBe(false)

    // Retry re-invokes createPipeline; the second call succeeds.
    await retry.trigger('click')
    await flushPromises()

    expect(postCalls).toBe(2)
    expect(wrapper.find('[data-testid="error-alert-retry"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Test Pipeline')
    expect(wrapper.find('[data-testid="library-wizard-view-pipeline"]').exists()).toBe(true)
  })
})
