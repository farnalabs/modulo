import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('../composables/useApi', () => ({
  useApi: vi.fn(() => ({
    get: vi.fn().mockResolvedValue({
      id: '1',
      email: 'user@example.com',
      display_name: 'Test User',
      org_role: 'admin',
      active: true,
      created_at: '2025-01-01T00:00:00Z',
    }),
    put: vi.fn().mockResolvedValue(undefined),
  })),
}))

import MyProfileView from '../views/MyProfileView.vue'

describe('MyProfileView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(MyProfileView)
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('My Profile')
  })
})
