import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
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
}))

import LibraryPipelineWizard from '../views/LibraryPipelineWizard.vue'

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
})
