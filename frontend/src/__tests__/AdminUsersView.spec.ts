import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  getAccessToken: vi.fn().mockReturnValue('eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbkBleGFtcGxlLmNvbSJ9.AAA'),
}))

import AdminUsersView from '../views/AdminUsersView.vue'

describe('AdminUsersView', () => {
  it('renders without crashing', async () => {
    const wrapper = mount(AdminUsersView)
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Users')
  })
})
