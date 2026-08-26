import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('vue-router', () => ({
  useRouter: vi.fn(() => ({ push: vi.fn() })),
  useRoute: vi.fn(() => ({ name: 'login' })),
}))

import LoginView from '../views/LoginView.vue'

function okJson(data: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => data,
  } as unknown as Response
}

function notAvailable() {
  return {
    ok: false,
    status: 402,
    statusText: 'Payment Required',
    json: async () => ({ detail: 'This feature is not available on your plan' }),
  } as unknown as Response
}

describe('LoginView', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders without crashing', async () => {
    const fetchMock = vi.mocked(globalThis.fetch)
    fetchMock.mockResolvedValue(okJson({ oidc: [], saml: false }))
    const wrapper = mount(LoginView)
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Modulo')
    expect(wrapper.text()).toContain('Sign in')
  })

  it('renders an SSO section when the instance advertises configured providers', async () => {
    const fetchMock = vi.mocked(globalThis.fetch)
    fetchMock.mockResolvedValue(
      okJson({ oidc: [{ provider_id: 'google' }, { provider_id: 'okta' }], saml: true }),
    )
    const wrapper = mount(LoginView)
    await flushPromises()

    expect(wrapper.find('[data-testid="login-sso-section"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="login-sso-oidc-google"]').attributes('href')).toBe(
      '/api/v1/auth/oidc/google/login',
    )
    expect(wrapper.get('[data-testid="login-sso-oidc-okta"]').attributes('href')).toBe(
      '/api/v1/auth/oidc/okta/login',
    )
    expect(wrapper.get('[data-testid="login-sso-saml"]').attributes('href')).toBe('/api/v1/auth/saml/login')
  })

  it('hides the SSO section when the sso feature is not available', async () => {
    const fetchMock = vi.mocked(globalThis.fetch)
    fetchMock.mockResolvedValue(notAvailable())
    const wrapper = mount(LoginView)
    await flushPromises()

    expect(wrapper.find('[data-testid="login-sso-section"]').exists()).toBe(false)
  })

  it('hides the SSO section when the instance advertises no providers', async () => {
    const fetchMock = vi.mocked(globalThis.fetch)
    fetchMock.mockResolvedValue(okJson({ oidc: [], saml: false }))
    const wrapper = mount(LoginView)
    await flushPromises()

    expect(wrapper.find('[data-testid="login-sso-section"]').exists()).toBe(false)
  })

  it('fails closed to password login when provider discovery errors', async () => {
    const fetchMock = vi.mocked(globalThis.fetch)
    fetchMock.mockRejectedValue(new Error('network down'))
    const wrapper = mount(LoginView)
    await flushPromises()

    expect(wrapper.find('[data-testid="login-sso-section"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="login-submit"]').exists()).toBe(true)
  })
})
