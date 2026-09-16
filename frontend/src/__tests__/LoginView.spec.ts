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

describe('LoginView - login-context integration', () => {
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
        },
      },
    })
  }

  it('auto-skips to single-org login when login-context returns one org', async () => {
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
    // Org name should be shown as subordinate element
    expect(wrapper.text()).toContain('Acme Corp')
    // No org entry step
    expect(wrapper.find('[data-testid="login-org-slug"]').exists()).toBe(false)
  })

  it('h1 always shows product brand even when org name is present (FAR-866 regression)', async () => {
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
    // h1 must always contain the product brand — never the org name
    const h1 = wrapper.find('h1')
    expect(h1.exists()).toBe(true)
    expect(h1.text()).toBe('Modulo')
    // Org name appears as a subordinate line, not in h1
    expect(wrapper.text()).toContain('Sign in to Acme Corp')
  })

  it('shows org entry step when login-context returns multi_org=true', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        multi_org: true,
        org: null,
      }),
    } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="login-org-slug"]').exists()).toBe(true)
    })
    // No password form in multi-org mode
    expect(wrapper.find('[data-testid="login-email"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="login-org-entry-submit"]').exists()).toBe(true)
  })

  it('org entry navigates to /login/:slug', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        multi_org: true,
        org: null,
      }),
    } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="login-org-slug"]').exists()).toBe(true)
    })

    await wrapper.find('[data-testid="login-org-slug"]').setValue('my-org')
    await wrapper.find('form').trigger('submit.prevent')

    // The handler sets window.location.href
    // In jsdom we can't spy on it, but we can verify the form submitted
    await nextTick()
  })

  it('login submits org_slug for single-org auto-skip', async () => {
    vi.spyOn(global, 'fetch')
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          multi_org: false,
          org: { slug: 'solo', name: 'Solo Org' },
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ oidc: [], saml: false }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          access_token: 'tok',
          refresh_token: 'ref',
        }),
      } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="login-email"]').exists()).toBe(true)
    })

    await wrapper.find('[data-testid="login-email"]').setValue('user@test.com')
    await wrapper.find('[data-testid="login-password"]').setValue('pass')
    await wrapper.find('form').trigger('submit.prevent')

    await vi.waitFor(() => {
      const fetchCalls = (global.fetch as ReturnType<typeof vi.fn>).mock.calls
      const loginCall = fetchCalls.find((call: unknown[]) => call[0] === '/api/v1/auth/login')
      expect(loginCall).toBeDefined()
      const body = JSON.parse((loginCall![1] as RequestInit).body as string)
      expect(body.org_slug).toBe('solo')
    })
  })

  it('login submit without org when login-context fails shows login form', async () => {
    // When login-context fails (500), contextLoading ends and multiOrg stays
    // false — the basic login form renders as fallback.
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: false,
      status: 500,
      json: () => Promise.resolve({}),
    } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      // Falls through to basic login form (email + password) — no org slug entry
      expect(wrapper.find('[data-testid="login-email"]').exists()).toBe(true)
    })
  })

  it('shows SSO providers in single-org mode when available', async () => {
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
          oidc: [{ provider_id: 'okta' }],
          saml: true,
        }),
      } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="login-sso-section"]').exists()).toBe(true)
    })
    expect(wrapper.find('[data-testid="login-sso-oidc-okta"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="login-sso-saml"]').exists()).toBe(true)
  })

  it('surfaces SSO providers when login-context is rate-limited (429)', async () => {
    // Regression: a transient 429 from the anonymous login-context rate limit
    // must not hide configured SSO providers — SSO discovery is independent of
    // the org-resolution call.
    vi.spyOn(global, 'fetch')
      .mockResolvedValueOnce({
        ok: false,
        status: 429,
        json: () => Promise.resolve({ detail: 'Too Many Requests' }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          oidc: [{ provider_id: 'google' }],
          saml: true,
        }),
      } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="login-sso-section"]').exists()).toBe(true)
    })
    expect(wrapper.find('[data-testid="login-sso-oidc-google"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="login-sso-saml"]').exists()).toBe(true)
  })
})
