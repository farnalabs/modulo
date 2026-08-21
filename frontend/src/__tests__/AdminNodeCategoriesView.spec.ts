import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const mockGet = vi.fn()
const mockDelete = vi.fn()

vi.mock('../lib/api/client', () => ({
  api: {
    GET: (...args: unknown[]) => mockGet(...args),
    POST: vi.fn(),
    PUT: vi.fn(),
    PATCH: vi.fn(),
    DELETE: (...args: unknown[]) => mockDelete(...args),
  },
}))

import AdminNodeCategoriesView from '../views/AdminNodeCategoriesView.vue'

describe('AdminNodeCategoriesView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    mockGet.mockResolvedValue({ data: { items: [] }, error: null })
    mockDelete.mockResolvedValue({ response: { status: 204 }, error: null })
  })

  it('renders without crashing', async () => {
    const wrapper = mount(AdminNodeCategoriesView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          NodeCategoryEditor: true,
          FeatureGate: { template: '<div><slot /></div>' },
        },
      },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Node Categories')
  })
})
