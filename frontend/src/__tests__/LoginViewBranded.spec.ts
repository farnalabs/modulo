import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../lib/api/client', () => ({
  getAccessToken: vi.fn().mockReturnValue(null),
  setAccessToken: vi.fn(),
  setRefreshToken: vi.fn(),
}))

vi.mock('../lib/mustChangePassword', () => ({
  setMustChangePassword: vi.fn(),
}))

import LoginView from '../views/LoginView.vue'

describe('LoginView - branded SSO buttons', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  function mountView() {
    return mount(LoginView, {
      global: {
        stubs: {
          Button: { template: '<button><slot /></button>' },
          SsoBrandMark: { template: '<span data-testid="brand-mark-stub" />', props: ['preset'] },
        },
      },
    })
  }

  it('renders "Sign in with {display_name}" for OIDC providers', async () => {
    vi.spyOn(global, 'fetch')
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          multi_org: false,
          org: { slug: 'acme', name: 'Acme Corp' },
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          oidc: [{ provider_id: 'google-oidc', display_name: 'Google Workspace', preset: 'google' }],
          saml: false,
        }),
      } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="login-sso-section"]').exists()).toBe(true)
    })
    const ssoBtn = wrapper.find('[data-testid="login-sso-oidc-google-oidc"]')
    expect(ssoBtn.exists()).toBe(true)
    expect(ssoBtn.text()).toContain('Sign in with Google Workspace')
  })

  it('shows brand mark for known presets', async () => {
    vi.spyOn(global, 'fetch')
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          multi_org: false,
          org: { slug: 'acme', name: 'Acme Corp' },
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          oidc: [{ provider_id: 'okta-oidc', display_name: 'Okta', preset: 'okta' }],
          saml: false,
        }),
      } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="login-sso-section"]').exists()).toBe(true)
    })
    expect(wrapper.find('[data-testid="brand-mark-stub"]').exists()).toBe(true)
  })

  it('falls back to provider_id when display_name is empty', async () => {
    vi.spyOn(global, 'fetch')
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          multi_org: false,
          org: { slug: 'acme', name: 'Acme Corp' },
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          oidc: [{ provider_id: 'my-sso', display_name: '', preset: 'custom' }],
          saml: false,
        }),
      } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="login-sso-section"]').exists()).toBe(true)
    })
    expect(wrapper.text()).toContain('Sign in with my-sso')
  })

  it('h1 always shows "Modulo" (FAR-866 regression)', async () => {
    vi.spyOn(global, 'fetch')
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          multi_org: false,
          org: { slug: 'acme', name: 'Acme Corp' },
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ oidc: [], saml: false }),
      } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="login-email"]').exists()).toBe(true)
    })
    const h1 = wrapper.find('h1')
    expect(h1.text()).toBe('Modulo')
  })
})
