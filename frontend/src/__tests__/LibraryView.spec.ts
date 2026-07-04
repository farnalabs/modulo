import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'

const mockGet = vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 12 })

vi.mock('../composables/useApi', () => ({
  useApi: vi.fn(() => ({
    get: mockGet,
    patch: vi.fn(),
  })),
}))

import LibraryView from '../views/LibraryView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/library', name: 'library', component: LibraryView },
    { path: '/library/:id/create-pipeline', name: 'library-pipeline-wizard', component: { template: '<div/>' } },
  ],
})

describe('LibraryView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockGet.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 12 })
  })

  it('renders without crashing', async () => {
    router.push('/library')
    await router.isReady()
    const wrapper = mount(LibraryView, {
      global: { plugins: [router] },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Library')
  })

  it('segregates preview primitives into a disclosure section and hides in-dev primitives', async () => {
    mockGet.mockResolvedValue({
      items: [
        { id: 'native-1', organisation_id: 'org', source: 'local', primitive_type: 'workflow', name: 'Native Workflow', slug: 'native-workflow', description: null, author: 'a', version: '1.0', tags: [], visibility: 'org', forked_from: null, auto_update: true, tier: 'native', created_at: '', updated_at: '' },
        { id: 'preview-1', organisation_id: 'org', source: 'local', primitive_type: 'workflow', name: 'Preview Workflow', slug: 'preview-workflow', description: null, author: 'a', version: '1.0', tags: [], visibility: 'org', forked_from: null, auto_update: true, tier: 'preview', created_at: '', updated_at: '' },
        { id: 'indev-1', organisation_id: 'org', source: 'local', primitive_type: 'workflow', name: 'InDev Workflow', slug: 'indev-workflow', description: null, author: 'a', version: '1.0', tags: [], visibility: 'org', forked_from: null, auto_update: true, tier: 'in_dev', created_at: '', updated_at: '' },
      ],
      total: 3,
      page: 1,
      page_size: 12,
    })

    router.push('/library')
    await router.isReady()
    const wrapper = mount(LibraryView, {
      global: { plugins: [router] },
    })
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('Native Workflow')
    expect(wrapper.text()).not.toContain('InDev Workflow')

    const previewSection = wrapper.find('[data-testid="library-preview-section"]')
    expect(previewSection.exists()).toBe(true)
    expect(previewSection.text()).toContain('Preview Workflow')
  })
})
