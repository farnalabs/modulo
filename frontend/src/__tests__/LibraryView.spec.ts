import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'

const getMock = vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 12 })

vi.mock('../composables/useApi', () => ({
  useApi: vi.fn(() => ({
    get: getMock,
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

const NATIVE_ITEM = {
  id: 'native-1',
  organisation_id: 'org-1',
  source: 'modulo',
  primitive_type: 'workflow',
  name: 'PRD to Requirements',
  slug: 'prd-to-requirements',
  description: null,
  author: 'modulo',
  version: '1.0',
  tags: [],
  visibility: 'community',
  forked_from: null,
  auto_update: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
}

const COMMUNITY_ITEM = {
  id: 'community-1',
  organisation_id: 'org-1',
  source: 'community',
  primitive_type: 'workflow',
  name: 'Translate to French',
  slug: 'translate-to-french',
  description: null,
  author: 'community',
  version: '1.0',
  tags: [],
  visibility: 'community',
  forked_from: null,
  auto_update: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
}

describe('LibraryView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    getMock.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 12 })
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
    getMock.mockResolvedValue({
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

  it('renders the Native section by default, excluding community items even if returned mixed', async () => {
    getMock.mockResolvedValue({ items: [NATIVE_ITEM, COMMUNITY_ITEM], total: 2, page: 1, page_size: 12 })
    router.push('/library')
    await router.isReady()
    const wrapper = mount(LibraryView, {
      global: { plugins: [router] },
    })
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('PRD to Requirements')
    expect(wrapper.text()).not.toContain('Translate to French')
  })

  it('renders the Community section separately with a non-verified indicator', async () => {
    getMock.mockResolvedValue({ items: [COMMUNITY_ITEM], total: 1, page: 1, page_size: 12 })
    router.push('/library')
    await router.isReady()
    const wrapper = mount(LibraryView, {
      global: { plugins: [router] },
    })
    await nextTick()

    await wrapper.get('[data-testid="library-section-community"]').trigger('click')
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('Translate to French')
    expect(wrapper.find('[data-testid="library-community-badge"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="library-community-badge"]').text()).toContain('not verified')
    expect(wrapper.find('[data-testid="library-community-disclaimer"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('PRD to Requirements')
  })
})
