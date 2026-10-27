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

const PRIMITIVE_WITH_SUB_GRAPH = {
  id: 'test-id-sub',
  primitive_type: 'pipeline_template',
  name: 'Composite Template',
  description: 'A composite template with sub-pipeline',
  author: 'Test Author',
  version: '1.0.0',
  tags: [],
  visibility: 'org',
  content_json: {
    agents: [],
    sub_pipeline_graph_json: {
      nodes: [
        { id: 'node-1', type: 'agent', position: { x: 100, y: 100 }, data: { label: 'Agent 1' } },
        { id: 'node-2', type: 'agent', position: { x: 300, y: 100 }, data: { label: 'Agent 2' } },
      ],
      edges: [
        { id: 'edge-1', source: 'node-1', target: 'node-2', type: 'smoothstep' },
      ],
    },
  },
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

    const retry = wrapper.find('[data-testid="error-alert-retry"]')
    expect(retry.exists()).toBe(true)
    expect(wrapper.text()).toContain('create backend down')
    expect(wrapper.find('[data-testid="library-wizard-view-pipeline"]').exists()).toBe(false)

    await retry.trigger('click')
    await flushPromises()

    expect(postCalls).toBe(2)
    expect(wrapper.find('[data-testid="error-alert-retry"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Test Pipeline')
    expect(wrapper.find('[data-testid="library-wizard-view-pipeline"]').exists()).toBe(true)
  })


  it('handles primitive load error and retry', async () => {
    vi.mocked(api.GET).mockResolvedValueOnce({ data: undefined, error: { detail: 'load failed' } } as never)
    router.push('/library/test-id/create-pipeline')
    await router.isReady()
    const wrapper = mount(LibraryPipelineWizard, { global: { plugins: [router], stubs: { OwnershipPicker: true } } })
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('load failed')
  })

  it('handles direct create API error and catch fallback', async () => {
    router.push('/library/test-id/create-pipeline')
    await router.isReady()
    const wrapper = mount(LibraryPipelineWizard, { global: { plugins: [router], stubs: { OwnershipPicker: true, VueFlow: true, Background: true } } })
    await flushPromises()

    vi.mocked(api.POST).mockResolvedValueOnce({ data: undefined, error: { detail: 'create error' } } as never)
    await wrapper.find('[data-testid="library-wizard-create"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('create error')

    vi.mocked(api.POST).mockRejectedValueOnce(new Error('transport error'))
    await wrapper.find('[data-testid="library-wizard-create"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('transport error')
  })

  it('renders sub-pipeline visualization for composites with sub_pipeline_graph_json', async () => {
    vi.mocked(api.GET).mockResolvedValue({ data: PRIMITIVE_WITH_SUB_GRAPH, error: undefined })

    router.push('/library/test-id-sub/create-pipeline')
    await router.isReady()
    const wrapper = mount(LibraryPipelineWizard, {
      global: {
        plugins: [router],
        stubs: { OwnershipPicker: true, VueFlow: true, Background: true },
      },
    })
    await flushPromises()
    await nextTick()

    // The sub-pipeline visualization section should be visible
    expect(wrapper.text()).toContain('Pipeline Visualisation')
    expect(wrapper.text()).toContain('sub-pipeline internal')
  })

  it('renders primitive tags', async () => {
    const primitiveWithTags = {
      ...PRIMITIVE,
      tags: ['tag1', 'tag2'],
    }
    vi.mocked(api.GET).mockResolvedValue({ data: primitiveWithTags, error: undefined })

    router.push('/library/test-id/create-pipeline')
    await router.isReady()
    const wrapper = mount(LibraryPipelineWizard, {
      global: {
        plugins: [router],
        stubs: { OwnershipPicker: true },
      },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('tag1')
    expect(wrapper.text()).toContain('tag2')
  })

  it('renders primitive author and version', async () => {
    router.push('/library/test-id/create-pipeline')
    await router.isReady()
    const wrapper = mount(LibraryPipelineWizard, {
      global: {
        plugins: [router],
        stubs: { OwnershipPicker: true },
      },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Test Author')
    expect(wrapper.text()).toContain('1.0.0')
  })

  it('filters sub-pipeline edges whose endpoints are absent from the laid-out nodes', async () => {
    vi.mocked(api.GET).mockResolvedValue({
      data: { ...PRIMITIVE_WITH_SUB_GRAPH, content_json: { sub_pipeline_graph_json: {
        nodes: [{ id: 'node-1', type: 'agent', position: { x: 0, y: 0 }, data: {} }],
        edges: [{ source: 'node-1', target: 'missing' }, { source: 'missing', target: 'node-1' }],
      } } },
      error: undefined,
    })
    router.push('/library/test-id-sub/create-pipeline')
    await router.isReady()
    const wrapper = mount(LibraryPipelineWizard, { global: { plugins: [router], stubs: { OwnershipPicker: true, VueFlow: true, Background: true } } })
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Pipeline Visualisation')
  })

  it('renders an empty template-agent list when the payload omits agents', async () => {
    vi.mocked(api.GET).mockResolvedValue({ data: { ...PRIMITIVE, content_json: {} }, error: undefined })
    router.push('/library/test-id/create-pipeline')
    await router.isReady()
    const wrapper = mount(LibraryPipelineWizard, { global: { plugins: [router], stubs: { OwnershipPicker: true } } })
    await flushPromises()
    expect(wrapper.exists()).toBe(true)
  })
})
