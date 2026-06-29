import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('vue-router', () => ({
  useRouter: vi.fn(() => ({ push: vi.fn() })),
  useRoute: vi.fn(() => ({ name: 'login' })),
}))

import LoginView from '../views/LoginView.vue'

describe('LoginView', () => {
  it('renders without crashing', async () => {
    const wrapper = mount(LoginView)
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Modulo')
    expect(wrapper.text()).toContain('Sign in')
  })
})
