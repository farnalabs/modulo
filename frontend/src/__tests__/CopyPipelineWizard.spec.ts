import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'

const mockGet = vi.fn().mockResolvedValue({
  items: [
    { id: 'p1', name: 'Prod Pipeline', description: 'Main production pipeline', visibility: 'org', created_at: '2025-01-15T00:00:00Z' },
    { id: 'p2', name: 'Dev Pipeline', description: 'Development pipeline', visibility: 'team', created_at: '2025-03-10T00:00:00Z' },
    { id: 'p3', name: 'Staging', description: null, visibility: 'org', created_at: '2025-02-20T00:00:00Z' },
  ],
  total: 3,
  page: 1,
  page_size: 100,
})

const mockPost = vi.fn().mockResolvedValue({
  id: 'new-pipeline-id',
  name: 'Copy of Prod Pipeline',
  description: null,
  visibility: 'org',
  created_at: '2025-06-30T00:00:00Z',
  updated_at: '2025-06-30T00:00:00Z',
})

vi.mock('../composables/useApi', () => ({
  useApi: vi.fn(() => ({
    get: mockGet,
    post: mockPost,
  })),
}))

vi.mock('../lib/api/client', () => ({
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
  api: {
    GET: vi.fn().mockResolvedValue({ data: { items: [] }, error: undefined }),
  },
}))

import CopyPipelineWizard from '../views/CopyPipelineWizard.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/pipelines/copy', name: 'pipeline-copy', component: CopyPipelineWizard },
    { path: '/pipelines/:id/editor', name: 'pipeline-editor', component: { template: '<div/>' } },
    { path: '/library', name: 'library', component: { template: '<div/>' } },
  ],
})

describe('CopyPipelineWizard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    router.push('/pipelines/copy')
    await router.isReady()
    const wrapper = mount(CopyPipelineWizard, {
      global: {
        plugins: [router],
        stubs: { OwnershipPicker: true },
      },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Copy Pipeline')
  })

  it('loads and displays pipelines in step 1', async () => {
    router.push('/pipelines/copy')
    await router.isReady()
    const wrapper = mount(CopyPipelineWizard, {
      global: {
        plugins: [router],
        stubs: { OwnershipPicker: true },
      },
    })
    await nextTick()
    await nextTick()
    expect(mockGet).toHaveBeenCalledWith('/api/v1/pipelines?page_size=100')
    expect(wrapper.text()).toContain('Prod Pipeline')
    expect(wrapper.text()).toContain('Dev Pipeline')
  })

  it('filters pipelines by search query', async () => {
    router.push('/pipelines/copy')
    await router.isReady()
    const wrapper = mount(CopyPipelineWizard, {
      global: {
        plugins: [router],
        stubs: { OwnershipPicker: true },
      },
    })
    await nextTick()
    await nextTick()

    const input = wrapper.find('[data-testid="copy-wizard-search"]')
    await input.setValue('Prod')
    expect(wrapper.text()).toContain('Prod Pipeline')
    expect(wrapper.text()).not.toContain('Dev Pipeline')
  })

  it('advances to step 2 when a pipeline is selected', async () => {
    router.push('/pipelines/copy')
    await router.isReady()
    const wrapper = mount(CopyPipelineWizard, {
      global: {
        plugins: [router],
        stubs: { OwnershipPicker: true },
      },
    })
    await nextTick()
    await nextTick()

    const options = wrapper.findAll('[data-testid="copy-wizard-pipeline-option"]')
    expect(options.length).toBeGreaterThan(0)
    await options[0].trigger('click')

    const nextBtn = wrapper.find('[data-testid="copy-wizard-next-step1"]')
    expect(nextBtn.attributes('disabled')).toBeUndefined()
    await nextBtn.trigger('click')

    expect(wrapper.text()).toContain('Copy Configuration')
    expect(wrapper.text()).toContain('New Pipeline Name')
  })

  it('shows review step with correct summary', async () => {
    router.push('/pipelines/copy')
    await router.isReady()
    const wrapper = mount(CopyPipelineWizard, {
      global: {
        plugins: [router],
        stubs: { OwnershipPicker: true },
      },
    })
    await nextTick()
    await nextTick()

    const options = wrapper.findAll('[data-testid="copy-wizard-pipeline-option"]')
    await options[0].trigger('click')
    await wrapper.find('[data-testid="copy-wizard-next-step1"]').trigger('click')

    const nameInput = wrapper.find('[data-testid="copy-wizard-pipeline-name"]')
    await nameInput.setValue('My Custom Copy')

    await wrapper.find('[data-testid="copy-wizard-next-step2"]').trigger('click')

    expect(wrapper.text()).toContain('Review Copy')
    expect(wrapper.text()).toContain('Prod Pipeline')
    expect(wrapper.text()).toContain('My Custom Copy')
  })

  it('executes copy and shows success', async () => {
    vi.useFakeTimers()
    router.push('/pipelines/copy')
    await router.isReady()
    const wrapper = mount(CopyPipelineWizard, {
      global: {
        plugins: [router],
        stubs: { OwnershipPicker: true },
      },
    })
    await nextTick()
    await nextTick()

    const options = wrapper.findAll('[data-testid="copy-wizard-pipeline-option"]')
    await options[0].trigger('click')
    await wrapper.find('[data-testid="copy-wizard-next-step1"]').trigger('click')
    await wrapper.find('[data-testid="copy-wizard-next-step2"]').trigger('click')

    const executeBtn = wrapper.find('[data-testid="copy-wizard-execute"]')
    await executeBtn.trigger('click')

    await vi.advanceTimersByTimeAsync(300)
    await nextTick()
    await vi.advanceTimersByTimeAsync(400)
    await nextTick()

    expect(mockPost).toHaveBeenCalled()
    expect(mockPost.mock.calls[0][0]).toContain('/clone')
    expect(mockPost.mock.calls[0][1]).toHaveProperty('name')
    vi.useRealTimers()
  })

  it('handles copy failure with retry option', async () => {
    vi.useFakeTimers()
    mockPost.mockRejectedValueOnce(new Error('Server error'))

    router.push('/pipelines/copy')
    await router.isReady()
    const wrapper = mount(CopyPipelineWizard, {
      global: {
        plugins: [router],
        stubs: { OwnershipPicker: true },
      },
    })
    await nextTick()
    await nextTick()

    const options = wrapper.findAll('[data-testid="copy-wizard-pipeline-option"]')
    await options[0].trigger('click')
    await wrapper.find('[data-testid="copy-wizard-next-step1"]').trigger('click')
    await wrapper.find('[data-testid="copy-wizard-next-step2"]').trigger('click')

    await wrapper.find('[data-testid="copy-wizard-execute"]').trigger('click')

    await vi.advanceTimersByTimeAsync(1000)
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('Copy Failed')
    expect(wrapper.find('[data-testid="copy-wizard-retry"]').exists()).toBe(true)
    vi.useRealTimers()
  })
})
