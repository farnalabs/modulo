import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const routerReplace = vi.fn()
const setMustChangePassword = vi.fn()
const clearAccessToken = vi.fn()

vi.mock('vue-router', () => ({
  useRouter: () => ({ replace: routerReplace }),
  useRoute: () => ({ meta: {} }),
  createRouter: vi.fn(),
  createWebHistory: vi.fn(),
}))

vi.mock('../../lib/api/client', () => ({
  clearAccessToken: () => clearAccessToken(),
  getAccessToken: vi.fn(() => 'token'),
}))

vi.mock('../../lib/mustChangePassword', () => ({
  setMustChangePassword: (v: boolean) => setMustChangePassword(v),
}))

const ChangePasswordFormStub = {
  name: 'ChangePasswordForm',
  props: ['quiet'],
  template: '<form @submit.prevent><button type="button" data-testid="cpf-changed" @click="$emit(\'changed\')">changed</button></form>',
  emits: ['changed'],
}

import ForceChangePasswordView from '../../views/ForceChangePasswordView.vue'

describe('ForceChangePasswordView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders the forced-password-change gate and sign-out escape hatch', () => {
    const wrapper = mount(ForceChangePasswordView, {
      global: { stubs: { ChangePasswordForm: ChangePasswordFormStub } },
    })
    expect(wrapper.find('[data-testid="force-change-password-sign-out"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Set a new password')
  })

  it('signs the user out immediately when the escape hatch is clicked', async () => {
    const wrapper = mount(ForceChangePasswordView, {
      global: { stubs: { ChangePasswordForm: ChangePasswordFormStub } },
    })
    await wrapper.find('[data-testid="force-change-password-sign-out"]').trigger('click')
    expect(setMustChangePassword).toHaveBeenCalledWith(false)
    expect(clearAccessToken).toHaveBeenCalled()
    expect(routerReplace).toHaveBeenCalledWith('/login')
  })

  it('shows a success announcement after the password is changed', async () => {
    const wrapper = mount(ForceChangePasswordView, {
      global: { stubs: { ChangePasswordForm: ChangePasswordFormStub } },
    })
    await wrapper.find('[data-testid="cpf-changed"]').trigger('click')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[aria-live="polite"]').exists()).toBe(true)
  })

  it('keeps the success announcement until the delayed session exit completes', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mount(ForceChangePasswordView, {
        global: { stubs: { ChangePasswordForm: ChangePasswordFormStub } },
      })
      await wrapper.find('[data-testid="cpf-changed"]').trigger('click')
      expect(routerReplace).not.toHaveBeenCalled()

      vi.advanceTimersByTime(1499)
      expect(routerReplace).not.toHaveBeenCalled()
      vi.advanceTimersByTime(1)

      expect(setMustChangePassword).toHaveBeenCalledWith(false)
      expect(clearAccessToken).toHaveBeenCalled()
      expect(routerReplace).toHaveBeenCalledWith('/login')
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('cancels delayed navigation when sign out is clicked during the announcement', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mount(ForceChangePasswordView, {
        global: { stubs: { ChangePasswordForm: ChangePasswordFormStub } },
      })
      await wrapper.find('[data-testid="cpf-changed"]').trigger('click')
      await wrapper.find('[data-testid="force-change-password-sign-out"]').trigger('click')
      vi.advanceTimersByTime(1500)

      expect(setMustChangePassword).toHaveBeenCalledTimes(1)
      expect(clearAccessToken).toHaveBeenCalledTimes(1)
      expect(routerReplace).toHaveBeenCalledTimes(1)
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('clears a pending delayed exit when the gate is unmounted', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mount(ForceChangePasswordView, {
        global: { stubs: { ChangePasswordForm: ChangePasswordFormStub } },
      })
      await wrapper.find('[data-testid="cpf-changed"]').trigger('click')
      wrapper.unmount()
      vi.advanceTimersByTime(1500)

      expect(routerReplace).not.toHaveBeenCalled()
      expect(clearAccessToken).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })
})
