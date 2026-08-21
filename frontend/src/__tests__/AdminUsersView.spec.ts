import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../lib/api/client', () => ({
  getAccessToken: vi.fn().mockReturnValue('eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbkBleGFtcGxlLmNvbSJ9.AAA'),
}))

import AdminUsersView from '../views/AdminUsersView.vue'

describe('AdminUsersView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('renders without crashing', async () => {
    const wrapper = mount(AdminUsersView, {
      global: {
        stubs: {
          FeatureGate: { template: '<div><slot /></div>' },
        },
      },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Users')
  })
})
