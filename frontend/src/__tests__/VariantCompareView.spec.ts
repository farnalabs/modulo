import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') return Promise.resolve({ data: [], error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    }),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import VariantCompareView from '../views/VariantCompareView.vue'

describe('VariantCompareView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(VariantCompareView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await nextTick()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Variant Comparison')
  })
})
