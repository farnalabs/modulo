import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'

vi.mock('../composables/useApi', () => ({
  useApi: vi.fn(() => ({
    get: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 12 }),
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
})
