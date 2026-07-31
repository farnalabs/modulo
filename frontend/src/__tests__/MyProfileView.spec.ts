import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

const { mockPut, mockGet } = vi.hoisted(() => {
  const mockPut = vi.fn().mockResolvedValue({ data: {}, error: undefined })
  const mockGet = vi.fn().mockResolvedValue({
    data: {
      id: '1',
      email: 'user@example.com',
      display_name: 'Test User',
      org_role: 'admin',
      active: true,
      created_at: '2025-01-01T00:00:00Z',
      is_system_admin: false,
    },
    error: undefined,
  })
  return { mockPut, mockGet }
})

vi.mock('../lib/api/client', () => ({
  api: {
    GET: mockGet,
    PUT: mockPut,
  },
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

  it('shows error when passwords do not match', async () => {
    const wrapper = mount(MyProfileView)
    await nextTick()

    const currentInput = wrapper.find('[data-testid="my-profile-current-password"]')
    const newInput = wrapper.find('[data-testid="my-profile-new-password"]')
    const confirmInput = wrapper.find('[data-testid="my-profile-confirm-password"]')

    await currentInput.setValue('old-password')
    await newInput.setValue('new-password')
    await confirmInput.setValue('different-password')

    await wrapper.find('[data-testid="my-profile-update-password"]').trigger('submit')

    expect(wrapper.text()).toContain('Passwords do not match')
    expect(mockPut).not.toHaveBeenCalled()
  })

  it('shows error when new password is too short', async () => {
    const wrapper = mount(MyProfileView)
    await nextTick()

    const currentInput = wrapper.find('[data-testid="my-profile-current-password"]')
    const newInput = wrapper.find('[data-testid="my-profile-new-password"]')
    const confirmInput = wrapper.find('[data-testid="my-profile-confirm-password"]')

    await currentInput.setValue('old-password')
    await newInput.setValue('short')
    await confirmInput.setValue('short')

    await wrapper.find('[data-testid="my-profile-update-password"]').trigger('submit')

    expect(wrapper.text()).toContain('at least 8 characters')
    expect(mockPut).not.toHaveBeenCalled()
  })

  it('successfully changes password', async () => {
    const wrapper = mount(MyProfileView)
    await nextTick()

    const currentInput = wrapper.find('[data-testid="my-profile-current-password"]')
    const newInput = wrapper.find('[data-testid="my-profile-new-password"]')
    const confirmInput = wrapper.find('[data-testid="my-profile-confirm-password"]')

    await currentInput.setValue('old-password')
    await newInput.setValue('new-strong-password-42')
    await confirmInput.setValue('new-strong-password-42')

    await wrapper.find('[data-testid="my-profile-update-password"]').trigger('submit')
    await nextTick()

    expect(mockPut).toHaveBeenCalledWith('/api/v1/me/password', {
      body: {
        current_password: 'old-password',
        new_password: 'new-strong-password-42',
      },
    })
    expect(wrapper.text()).toContain('Password changed successfully')
  })

  it('shows API error message on failure', async () => {
    mockPut.mockResolvedValueOnce({ data: undefined, error: 'Current password is incorrect' })

    const wrapper = mount(MyProfileView)
    await nextTick()

    const currentInput = wrapper.find('[data-testid="my-profile-current-password"]')
    const newInput = wrapper.find('[data-testid="my-profile-new-password"]')
    const confirmInput = wrapper.find('[data-testid="my-profile-confirm-password"]')

    await currentInput.setValue('wrong-password')
    await newInput.setValue('new-strong-password-42')
    await confirmInput.setValue('new-strong-password-42')

    await wrapper.find('[data-testid="my-profile-update-password"]').trigger('submit')
    await nextTick()

    expect(wrapper.text()).toContain('Current password is incorrect')
  })
})
