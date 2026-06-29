import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'

vi.mock('../composables/useApi', () => ({
  useApi: vi.fn(() => ({
    get: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 12 }),
    post: vi.fn().mockResolvedValue({ pipeline_id: 'abc-123', pipeline_name: 'Test', agent_count: 0, edge_count: 0 }),
  })),
}))

import PipelineTemplateGallery from '../views/PipelineTemplateGallery.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/templates', name: 'pipeline-templates', component: PipelineTemplateGallery },
    { path: '/pipelines/:id/editor', name: 'pipeline-editor', component: { template: '<div/>' } },
  ],
})

describe('PipelineTemplateGallery', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    router.push('/templates')
    await router.isReady()
    const wrapper = mount(PipelineTemplateGallery, {
      global: { plugins: [router] },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Pipeline Templates')
  })

  it('renders category tabs', async () => {
    router.push('/templates')
    await router.isReady()
    const wrapper = mount(PipelineTemplateGallery, {
      global: { plugins: [router] },
    })
    await nextTick()
    const tabs = wrapper.findAll('[data-testid="template-gallery-category-tab"]')
    expect(tabs.length).toBe(6)
    expect(tabs[0].text()).toContain('All')
    expect(tabs[1].text()).toContain('SDLC')
    expect(tabs[2].text()).toContain('DevOps')
    expect(tabs[3].text()).toContain('Security')
    expect(tabs[4].text()).toContain('Data')
    expect(tabs[5].text()).toContain('Custom')
  })

  it('shows loading skeleton while fetching', async () => {
    router.push('/templates')
    await router.isReady()
    const wrapper = mount(PipelineTemplateGallery, {
      global: { plugins: [router] },
    })
    expect(wrapper.find('[data-testid="template-gallery-skeleton"]').exists()).toBe(true)
  })

  it('renders search input', async () => {
    router.push('/templates')
    await router.isReady()
    const wrapper = mount(PipelineTemplateGallery, {
      global: { plugins: [router] },
    })
    const searchInput = wrapper.find('[data-testid="template-gallery-search"]')
    expect(searchInput.exists()).toBe(true)
    expect(searchInput.attributes('placeholder')).toBe('Search templates...')
  })
})
