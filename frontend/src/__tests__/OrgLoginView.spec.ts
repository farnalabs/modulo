import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import type { components } from '../lib/api/schema'

// Mock vue-router with actual route params
const mockRoute = {
  path: '/login/test-org',
  fullPath: '/login/test-org',
  params: { slug: 'test-org' },
  query: {},
  hash: '',
  matched: [],
  name: 'org-login',
  redirectedFrom: undefined,
}

const mockRouter = {
  push: vi.fn(),
  replace: vi.fn(),
  currentRoute: { value: mockRoute },
}

vi.mock('vue-router', () => ({
  useRoute: vi.fn(() => mockRoute),
  useRouter: vi.fn(() => mockRouter),
}))

vi.mock('../lib/api/client', () => ({
  getAccessToken: vi.fn().mockReturnValue(null),
  setAccessToken: vi.fn(),
  setRefreshToken: vi.fn(),
}))

vi.mock('../lib/mustChangePassword', () => ({
  setMustChangePassword: vi.fn(),
}))

import OrgLoginView from '../views/OrgLoginView.vue'

describe('OrgLoginView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  function mountView(slug = 'test-org') {
    mockRoute.params.slug = slug
    return mount(OrgLoginView, {
      global: {
        stubs: {
          Button: { template: '<button><slot /></button>' },
        },
      },
    })
  }

  it('shows loading state initially', () => {
    // Never resolve fetch so we stay in loading
    vi.spyOn(global, 'fetch').mockReturnValue(new Promise(() => {}))
    const wrapper = mountView()
    expect(wrapper.find('[data-testid="org-login-loading"]').exists()).toBe(true)
  })

  it('shows not-found when org-login returns 404', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: false,
      status: 404,
      json: () => Promise.resolve({ detail: 'Not Found' }),
    } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="org-login-not-found"]').exists()).toBe(true)
    })
    expect(wrapper.text()).toContain('Organisation not found')
  })

  it('renders org name and password form when org is found', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        org: { slug: 'test-org', name: 'Test Org' },
        providers: [],
        password_enabled: true,
      }),
    } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="org-login-email"]').exists()).toBe(true)
    })
    expect(wrapper.text()).toContain('Test Org')
    expect(wrapper.find('[data-testid="org-login-password"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="org-login-submit"]').exists()).toBe(true)
  })

  it('h1 always shows product brand even when org name is present (FAR-866 regression)', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        org: { slug: 'test-org', name: 'Test Org' },
        providers: [],
        password_enabled: true,
      }),
    } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="org-login-email"]').exists()).toBe(true)
    })
    // h1 must always contain the product brand — never the org name
    const h1 = wrapper.find('h1')
    expect(h1.exists()).toBe(true)
    expect(h1.text()).toBe('Modulo')
    // Org name appears as a subordinate line, not in h1
    expect(wrapper.text()).toContain('Sign in to Test Org')
  })

  it('hides password form when password is disabled', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        org: { slug: 'test-org', name: 'Test Org' },
        providers: [{ provider_id: 'github', display_name: 'GitHub' }],
        password_enabled: false,
      }),
    } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="org-login-sso-section"]').exists()).toBe(true)
    })
    expect(wrapper.find('[data-testid="org-login-email"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="org-login-submit"]').exists()).toBe(false)
  })

  it('shows SSO provider buttons when providers exist', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        org: { slug: 'test-org', name: 'Test Org' },
        providers: [
          { provider_id: 'github', display_name: 'GitHub' },
          { provider_id: 'google', display_name: 'Google' },
        ],
        password_enabled: true,
      }),
    } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="org-login-sso-section"]').exists()).toBe(true)
    })
    expect(wrapper.find('[data-testid="org-login-sso-github"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="org-login-sso-google"]').exists()).toBe(true)
    // OIDC link must NOT include a dead ?org= query param — provider_id is globally unique
    const githubLink = wrapper.find('[data-testid="org-login-sso-github"]')
    expect(githubLink.attributes('href')).toBe('/api/v1/auth/oidc/github/login')
    expect(githubLink.attributes('href')).not.toContain('?')
  })

  it('login submit includes org_slug in the request', async () => {
    vi.spyOn(global, 'fetch')
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          org: { slug: 'test-org', name: 'Test Org' },
          providers: [],
          password_enabled: true,
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          access_token: 'test-token',
          refresh_token: 'test-refresh',
        }),
      } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="org-login-email"]').exists()).toBe(true)
    })

    await wrapper.find('[data-testid="org-login-email"]').setValue('admin@example.com')
    await wrapper.find('[data-testid="org-login-password"]').setValue('password123')
    await wrapper.find('form').trigger('submit.prevent')

    await vi.waitFor(() => {
      const fetchCalls = (global.fetch as ReturnType<typeof vi.fn>).mock.calls
      const loginCall = fetchCalls.find((call: unknown[]) => call[0] === '/api/v1/auth/login')
      expect(loginCall).toBeDefined()
      const body = JSON.parse((loginCall![1] as RequestInit).body as string)
      expect(body.org_slug).toBe('test-org')
      expect(body.email).toBe('admin@example.com')
      expect(body.password).toBe('password123')
    })
  })

  it('shows generic error on login failure', async () => {
    vi.spyOn(global, 'fetch')
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          org: { slug: 'test-org', name: 'Test Org' },
          providers: [],
          password_enabled: true,
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: false,
        status: 401,
        json: () => Promise.resolve({ detail: 'Invalid credentials' }),
      } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="org-login-email"]').exists()).toBe(true)
    })

    await wrapper.find('[data-testid="org-login-email"]').setValue('admin@example.com')
    await wrapper.find('[data-testid="org-login-password"]').setValue('wrong')
    // The form submit triggers useMutation which throws — catch the rejection
    // so vitest doesn't flag it as unhandled. The error is surfaced in the UI
    // via the loginError ref.
    const submitPromise = wrapper.find('form').trigger('submit.prevent')
    await submitPromise.catch(() => {})

    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="org-login-error"]').exists()).toBe(true)
      expect(wrapper.text()).toContain('Invalid credentials')
    })
  })

  it('renders SAML button when saml is true', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        org: { slug: 'test-org', name: 'Test Org' },
        providers: [
          { provider_id: 'okta-saml', display_name: 'Okta SAML', type: 'saml', preset: null },
        ],
        password_enabled: true,
        saml: true,
      }),
    } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="org-login-sso-okta-saml"]').exists()).toBe(true)
    })
    const samlLink = wrapper.find('[data-testid="org-login-sso-okta-saml"]')
    // Per-provider SAML login URL (FAR-1004)
    expect(samlLink.attributes('href')).toBe('/api/v1/auth/saml/okta-saml/login')
  })

  it('hides SAML button when saml is false and no SAML providers in list', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        org: { slug: 'test-org', name: 'Test Org' },
        providers: [],
        password_enabled: true,
        saml: false,
      }),
    } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="org-login-email"]').exists()).toBe(true)
    })
    expect(wrapper.find('[data-testid="org-login-sso-section"]').exists()).toBe(false)
  })

  it('shows SSO section when only SAML is available (no OIDC providers)', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        org: { slug: 'test-org', name: 'Test Org' },
        providers: [
          { provider_id: 'okta-saml', display_name: 'Okta SAML', type: 'saml', preset: null },
        ],
        password_enabled: true,
        saml: true,
      }),
    } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="org-login-sso-section"]').exists()).toBe(true)
    })
    expect(wrapper.find('[data-testid="org-login-sso-okta-saml"]').exists()).toBe(true)
  })

  it('renders OIDC and SAML providers with correct URLs (type discriminator)', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        org: { slug: 'test-org', name: 'Test Org' },
        providers: [
          { provider_id: 'google', display_name: 'Google', type: 'oidc', preset: 'google' },
          { provider_id: 'okta-saml', display_name: 'Okta SAML', type: 'saml', preset: null },
        ],
        password_enabled: true,
        saml: true,
      }),
    } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="org-login-sso-google"]').exists()).toBe(true)
    })
    // OIDC provider uses OIDC login URL — no dead ?org= param (provider_id is globally unique)
    const oidcLink = wrapper.find('[data-testid="org-login-sso-google"]')
    expect(oidcLink.attributes('href')).toBe('/api/v1/auth/oidc/google/login')
    // SAML provider uses per-provider SAML login URL
    const samlLink = wrapper.find('[data-testid="org-login-sso-okta-saml"]')
    expect(samlLink.attributes('href')).toBe('/api/v1/auth/saml/okta-saml/login')
  })

  it('response shape matches generated OrgLoginProviderInfo type (real types)', async () => {
    // This test imports the REAL generated types from schema.ts to verify
    // the mock response shape matches the actual API contract.
    type OrgLoginProviderInfo = components['schemas']['OrgLoginProviderInfo']
    type OrgLoginResponse = components['schemas']['OrgLoginResponse']

    const mockProviders: OrgLoginProviderInfo[] = [
      { provider_id: 'google', display_name: 'Google', type: 'oidc', preset: 'google' },
      { provider_id: 'okta-saml', display_name: 'Okta SAML', type: 'saml', preset: undefined },
    ]
    const mockResponse: OrgLoginResponse = {
      org: { slug: 'test-org', name: 'Test Org' },
      providers: mockProviders,
      password_enabled: true,
      saml: true,
    }

    // Verify the shape compiles against the real generated type
    expect(mockResponse.org.slug).toBe('test-org')
    expect(mockResponse.providers).toHaveLength(2)
    expect(mockResponse.providers[0].type).toBe('oidc')
    expect(mockResponse.providers[1].type).toBe('saml')
    expect(mockResponse.saml).toBe(true)

    // Now mount with this data and verify rendering
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(mockResponse),
    } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="org-login-sso-google"]').exists()).toBe(true)
    })
    expect(wrapper.find('[data-testid="org-login-sso-okta-saml"]').exists()).toBe(true)
  })
})
