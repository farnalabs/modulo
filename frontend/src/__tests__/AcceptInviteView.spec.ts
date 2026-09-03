import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../lib/api/client', () => ({
  getAccessToken: vi.fn().mockReturnValue(null),
}))

const mockPost = vi.fn()

vi.mock('../composables/useApi', () => ({
  useApi: () => ({
    get: vi.fn(),
    post: (...args: unknown[]) => mockPost(...(args as [])),
    put: vi.fn(),
    delete: vi.fn(),
  }),
}))

import AcceptInviteView from '../views/AcceptInviteView.vue'

describe('AcceptInviteView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    window.history.replaceState({}, '', '/accept-invite')
  })

  afterEach(() => {
    vi.useRealTimers()
    window.history.replaceState({}, '', '/')
  })

  function mountView() {
    return mount(AcceptInviteView, {
      global: {
        stubs: {
          FeatureGate: { template: '<div><slot /></div>' },
        },
      },
    })
  }

  it('shows an alert when the token is missing', async () => {
    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="accept-invite-missing-token"]').exists()).toBe(true)
    })
  })

  it('submits the token + password and shows success for a new account', async () => {
    // The one-time token is delivered in the URL FRAGMENT, not the query string.
    window.history.replaceState({}, '', '/accept-invite#token=tok-123')
    mockPost.mockResolvedValue({ detail: 'Invitation accepted', existing_account: false })
    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="accept-invite-submit"]').exists()).toBe(true)
    })
    // The secret is scrubbed from the address bar as soon as the view mounts.
    expect(window.location.hash).toBe('')
    expect(window.location.pathname).toBe('/accept-invite')
    await wrapper.find('[data-testid="accept-invite-password"]').setValue('C0rr3ct-Horse-Battery')
    await wrapper.find('[data-testid="accept-invite-confirm"]').setValue('C0rr3ct-Horse-Battery')
    await wrapper.find('form').trigger('submit.prevent')
    await vi.waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/api/v1/auth/accept-invite', {
        token: 'tok-123',
        password: 'C0rr3ct-Horse-Battery',
      })
      expect(wrapper.find('[data-testid="accept-invite-success"]').exists()).toBe(true)
    })
  })

  it('blocks submission client-side when passwords do not match', async () => {
    window.history.replaceState({}, '', '/accept-invite#token=tok-123')
    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="accept-invite-submit"]').exists()).toBe(true)
    })
    await wrapper.find('[data-testid="accept-invite-password"]').setValue('C0rr3ct-Horse-Battery')
    await wrapper.find('[data-testid="accept-invite-confirm"]').setValue('different123A')
    await wrapper.find('form').trigger('submit.prevent')
    await nextTick()
    expect(mockPost).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="accept-invite-error"]').exists()).toBe(true)
  })

  it('surfaces a server rejection inline', async () => {
    window.history.replaceState({}, '', '/accept-invite#token=expired-tok')
    mockPost.mockRejectedValue(new Error('Invalid or expired invitation'))
    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="accept-invite-submit"]').exists()).toBe(true)
    })
    await wrapper.find('[data-testid="accept-invite-password"]').setValue('C0rr3ct-Horse-Battery')
    await wrapper.find('[data-testid="accept-invite-confirm"]').setValue('C0rr3ct-Horse-Battery')
    await wrapper.find('form').trigger('submit.prevent')
    await vi.waitFor(() => {
      const err = wrapper.find('[data-testid="accept-invite-error"]')
      expect(err.exists()).toBe(true)
      expect(err.text()).toContain('Invalid or expired invitation')
    })
  })

  it('shows the existing-account success message when the email already has a local password', async () => {
    window.history.replaceState({}, '', '/accept-invite#token=tok-123')
    mockPost.mockResolvedValue({ detail: 'Invitation accepted', existing_account: true })
    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="accept-invite-submit"]').exists()).toBe(true)
    })
    await wrapper.find('[data-testid="accept-invite-password"]').setValue('C0rr3ct-Horse-Battery')
    await wrapper.find('[data-testid="accept-invite-confirm"]').setValue('C0rr3ct-Horse-Battery')
    await wrapper.find('form').trigger('submit.prevent')
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="accept-invite-success"]').exists()).toBe(true)
    })
    // Branch (d): the holder already had a local account, so the UI tells them
    // to sign in with their existing credentials rather than "check your email".
    expect(wrapper.text()).toContain('Membership added')
  })

  it('blocks submission when the password fails the strength rule', async () => {
    window.history.replaceState({}, '', '/accept-invite#token=tok-123')
    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="accept-invite-submit"]').exists()).toBe(true)
    })
    await wrapper.find('[data-testid="accept-invite-password"]').setValue('abc')
    await wrapper.find('[data-testid="accept-invite-confirm"]').setValue('abc')
    await wrapper.find('form').trigger('submit.prevent')
    await nextTick()
    expect(mockPost).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="accept-invite-error"]').exists()).toBe(true)
  })
})
