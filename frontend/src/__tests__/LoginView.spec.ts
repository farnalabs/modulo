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
    localStorage.clear()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
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

function fakeLocation(href: string): Location {
  return {
    pathname: '/login',
    href,
  } as unknown as Location
}

describe('LoginView - remembered org slug', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    localStorage.clear()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
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

  function mockMultiOrgContext() {
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ multi_org: true, org: null }),
    } as Response)
  }

  it('redirects straight to the remembered org login when a slug is stored', async () => {
    localStorage.setItem('modulo_login_last_org', 'acme')
    const location = fakeLocation('http://localhost/login')
    vi.stubGlobal('location', location)
    mockMultiOrgContext()

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(location.href).toBe('/login/acme')
    })
    // The slug-entry form must never flash while redirecting — loading stays up.
    expect(wrapper.find('[data-testid="login-org-slug"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="login-context-loading"]').exists()).toBe(true)
  })

  it('does not redirect when no org slug is stored', async () => {
    const location = fakeLocation('http://localhost/login')
    vi.stubGlobal('location', location)
    mockMultiOrgContext()

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="login-org-slug"]').exists()).toBe(true)
    })
    expect(location.href).toBe('http://localhost/login')
  })

  it('stores the entered slug before navigating to /login/:slug', async () => {
    const location = fakeLocation('http://localhost/login')
    vi.stubGlobal('location', location)
    mockMultiOrgContext()

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="login-org-slug"]').exists()).toBe(true)
    })

    await wrapper.find('[data-testid="login-org-slug"]').setValue('my-org')
    await wrapper.find('form').trigger('submit.prevent')

    expect(localStorage.getItem('modulo_login_last_org')).toBe('my-org')
    expect(location.href).toBe('/login/my-org')
  })

  it('still navigates on org entry when localStorage writes throw', async () => {
    const location = fakeLocation('http://localhost/login')
    vi.stubGlobal('location', location)
    // jsdom's localStorage is a Proxy that silently ignores instance-level
    // reassignment of getItem/setItem, so the throwing stub must be installed
    // on Storage.prototype for the failure path to actually execute.
    const setItemSpy = vi
      .spyOn(Storage.prototype, 'setItem')
      .mockImplementation(() => {
        throw new Error('quota exceeded')
      })
    mockMultiOrgContext()

    try {
      const wrapper = mountView()
      await vi.waitFor(() => {
        expect(wrapper.find('[data-testid="login-org-slug"]').exists()).toBe(true)
      })

      await wrapper.find('[data-testid="login-org-slug"]').setValue('doomed-org')
      await wrapper.find('form').trigger('submit.prevent')

      expect(location.href).toBe('/login/doomed-org')
    } finally {
      setItemSpy.mockRestore()
    }
  })

  it('still shows the org entry step when localStorage reads throw', async () => {
    const getItemSpy = vi
      .spyOn(Storage.prototype, 'getItem')
      .mockImplementation(() => {
        throw new Error('storage disabled')
      })
    mockMultiOrgContext()

    try {
      const wrapper = mountView()
      await vi.waitFor(() => {
        expect(wrapper.find('[data-testid="login-org-slug"]').exists()).toBe(true)
      })
      expect(wrapper.find('[data-testid="login-context-loading"]').exists()).toBe(false)
    } finally {
      getItemSpy.mockRestore()
    }
  })
})

describe('LoginView - used last time tag', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    localStorage.clear()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
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

  function mockSingleOrgContext(sso: { oidc: unknown[]; saml: boolean }) {
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
        json: () => Promise.resolve(sso),
      } as Response)
  }

  it('tags the password form when password was the last-used method', async () => {
    localStorage.setItem('modulo_login_last_method', 'password')
    mockSingleOrgContext({ oidc: [], saml: false })

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="login-email"]').exists()).toBe(true)
    })
    const tag = wrapper.find('[data-testid="login-last-used-password"]')
    expect(tag.exists()).toBe(true)
    expect(tag.text()).toBe('Used last time')
  })

  it('tags only the matching SSO provider button', async () => {
    localStorage.setItem('modulo_login_last_method', 'okta')
    mockSingleOrgContext({ oidc: [{ provider_id: 'okta' }, { provider_id: 'google' }], saml: false })

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="login-sso-section"]').exists()).toBe(true)
    })
    expect(wrapper.find('[data-testid="login-sso-oidc-okta"]').text()).toContain('Used last time')
    expect(wrapper.find('[data-testid="login-sso-oidc-google"]').text()).not.toContain('Used last time')
    expect(wrapper.find('[data-testid="login-last-used-password"]').exists()).toBe(false)
  })

  it('tags the SAML button when SAML was the last-used method', async () => {
    localStorage.setItem('modulo_login_last_method', 'saml')
    mockSingleOrgContext({ oidc: [{ provider_id: 'okta' }], saml: true })

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="login-sso-saml"]').exists()).toBe(true)
    })
    expect(wrapper.find('[data-testid="login-sso-saml"]').text()).toContain('Used last time')
    expect(wrapper.find('[data-testid="login-sso-oidc-okta"]').text()).not.toContain('Used last time')
    expect(wrapper.find('[data-testid="login-last-used-password"]').exists()).toBe(false)
  })

  it('records the SSO provider_id when an SSO button is activated', async () => {
    mockSingleOrgContext({ oidc: [{ provider_id: 'okta' }], saml: false })

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="login-sso-oidc-okta"]').exists()).toBe(true)
    })

    await wrapper.find('[data-testid="login-sso-oidc-okta"]').trigger('click')

    expect(localStorage.getItem('modulo_login_last_method')).toBe('okta')
  })

  it('records password as the last-used method when an SSO button is activated even if storage is read-only later', async () => {
    // Activation still updates the in-memory tag source even when a previous
    // session stored a different method — storage write itself is best-effort.
    localStorage.setItem('modulo_login_last_method', 'password')
    mockSingleOrgContext({ oidc: [{ provider_id: 'okta' }], saml: false })

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="login-sso-oidc-okta"]').exists()).toBe(true)
    })
    expect(wrapper.find('[data-testid="login-last-used-password"]').exists()).toBe(true)

    await wrapper.find('[data-testid="login-sso-oidc-okta"]').trigger('click')

    expect(localStorage.getItem('modulo_login_last_method')).toBe('okta')
    await nextTick()
    expect(wrapper.find('[data-testid="login-sso-oidc-okta"]').text()).toContain('Used last time')
    expect(wrapper.find('[data-testid="login-last-used-password"]').exists()).toBe(false)
  })

  it('shows no tag when the stored method is not available on this screen', async () => {
    localStorage.setItem('modulo_login_last_method', 'github')
    mockSingleOrgContext({ oidc: [], saml: false })

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="login-email"]').exists()).toBe(true)
    })
    expect(wrapper.text()).not.toContain('Used last time')
  })

  it('renders without a tag when localStorage reads throw', async () => {
    const getItemSpy = vi
      .spyOn(Storage.prototype, 'getItem')
      .mockImplementation(() => {
        throw new Error('storage disabled')
      })
    mockSingleOrgContext({ oidc: [], saml: false })

    try {
      const wrapper = mountView()
      await vi.waitFor(() => {
        expect(wrapper.find('[data-testid="login-email"]').exists()).toBe(true)
      })
      expect(wrapper.text()).not.toContain('Used last time')
    } finally {
      getItemSpy.mockRestore()
    }
  })

  it('records password as the last-used method after a successful login', async () => {
    mockSingleOrgContext({ oidc: [], saml: false })
    vi.spyOn(global, 'fetch').mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ access_token: 'tok', refresh_token: 'ref' }),
    } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="login-email"]').exists()).toBe(true)
    })

    await wrapper.find('[data-testid="login-email"]').setValue('user@test.com')
    await wrapper.find('[data-testid="login-password"]').setValue('pass')
    await wrapper.find('form').trigger('submit.prevent')

    await vi.waitFor(() => {
      expect(localStorage.getItem('modulo_login_last_method')).toBe('password')
    })
  })

  it('completes a successful login when localStorage writes throw', async () => {
    const setItemSpy = vi
      .spyOn(Storage.prototype, 'setItem')
      .mockImplementation(() => {
        throw new Error('quota exceeded')
      })
    mockSingleOrgContext({ oidc: [], saml: false })
    vi.spyOn(global, 'fetch').mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ access_token: 'tok', refresh_token: 'ref' }),
    } as Response)

    try {
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
      })
      expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    } finally {
      setItemSpy.mockRestore()
    }
  })
})
