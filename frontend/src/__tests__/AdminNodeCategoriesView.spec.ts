import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

const mockFetch = vi.fn()
global.fetch = mockFetch

import AdminNodeCategoriesView from '../views/AdminNodeCategoriesView.vue'

describe('AdminNodeCategoriesView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ items: [] }),
    })
  })

  it('renders without crashing', async () => {
    const wrapper = mount(AdminNodeCategoriesView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          NodeCategoryEditor: true,
        },
      },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Node Categories')
  })
})
