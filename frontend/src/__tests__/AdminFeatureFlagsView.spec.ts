import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({
      data: {
        license: { tier: 'free', has_license_key: false, is_valid: true },
        flags: [],
        would_activate: [],
      },
      error: undefined,
    }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminFeatureFlagsView from '../views/AdminFeatureFlagsView.vue'

describe('AdminFeatureFlagsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(AdminFeatureFlagsView, {
      global: { plugins: [createPinia()] },
    })
    await nextTick()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Feature Flags')
  })
})
