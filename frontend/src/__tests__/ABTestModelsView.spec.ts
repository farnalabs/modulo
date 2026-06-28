import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') return Promise.resolve({ data: [] as any, error: undefined })
      return Promise.resolve({ data: { items: [] as any, total: 0, page: 1, page_size: 50 }, error: undefined })
    }),
    POST: vi.fn().mockResolvedValue({ data: { id: '1' }, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import ABTestModelsView from '../views/ABTestModelsView.vue'

describe('ABTestModelsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(ABTestModelsView)
    await nextTick()
    await new Promise(r => setTimeout(r, 0))
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('A/B Test Models')
  })
})
