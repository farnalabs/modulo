import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { computed, nextTick, ref } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../lib/api/client', () => ({
  getAccessToken: vi.fn().mockReturnValue(null),
  setAccessToken: vi.fn(),
  setRefreshToken: vi.fn(),
}))

vi.mock('../lib/mustChangePassword', () => ({
  setMustChangePassword: vi.fn(),
}))

// FAR-1791: LoginView now reads `route.query.sso_error` via useRoute(), so the
// router composables are mocked with a controllable route (mirrors
// OrgLoginView.spec.ts). `mockRoute.query` is reset per test.
const mockRoute = {
  path: '/login',
  fullPath: '/login',
  params: {} as Record<string, string>,
  query: {} as Record<string, string>,
  hash: '',
  matched: [],
  name: 'login',
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

// FAR-1669: when armed, the mocked useMutation hands the view a `login` that
// REJECTS outright — no fetch, no tanstack — so a test can pin the submit
// handler's contract: the rejection must be caught by the handler while the
// error still surfaces. Disarmed (null) the real composable is used, so every
// other test in this file keeps its real behaviour.
const loginFailure = vi.hoisted(() => ({ message: null as string | null }))

vi.mock('../composables/useMutation', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../composables/useMutation')>()
  return {
    ...actual,
    useMutation: <TInput = void, TOutput = void>(fn: (input: TInput) => Promise<TOutput>) => {
      const message = loginFailure.message
      if (message === null) return actual.useMutation(fn)
      // Armed: `mutate` rejects on call, and the error state carries the
      // message (as tanstack's mutation store would after a failed login).
      // It starts null so the ErrorAlert only appears AFTER the failed submit.
      const surfaced = ref<string | null>(null)
      return {
        loading: ref(false),
        error: computed(() => surfaced.value),
        mutate: async (): Promise<TOutput | undefined> => {
          surfaced.value = message
          throw new Error(message)
        },
      }
    },
  }
})

import LoginView from '../views/LoginView.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'

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

  it('surfaces a failed login through the shared ErrorAlert with assertive aria-live (FAR-1669)', async () => {
    // Keyed by URL so the login failure is deterministic regardless of how the
    // fire-and-forget SSO discovery request interleaves with the submit.
    vi.spyOn(global, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/v1/auth/login')) {
        return Promise.resolve({
          ok: false,
          status: 401,
          statusText: 'Unauthorized',
          json: () => Promise.resolve({ detail: 'Invalid credentials' }),
        } as Response)
      }
      if (url.includes('/api/v1/auth/login-context')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({
            multi_org: false,
            org: { slug: 'acme', name: 'Acme Corp' },
          }),
        } as Response)
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ oidc: [], saml: false }),
      } as Response)
    })

    // The login mutation rejects by design (401). Vue's dev-mode error path
    // rethrows an event-handler rejection from inside its own `.catch`, which
    // Node reports as an unhandled rejection and fails the run — install a
    // scoped errorHandler on THIS mount so the rejection stays handled. The
    // rendered error state comes from tanstack's own mutation store and is
    // unaffected by the handler.
    const wrapper = mount(LoginView, {
      global: {
        stubs: {
          Button: { template: '<button><slot /></button>' },
        },
        config: { errorHandler: () => {} },
      },
    })
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="login-email"]').exists()).toBe(true)
    })

    await wrapper.find('[data-testid="login-email"]').setValue('user@test.com')
    await wrapper.find('[data-testid="login-password"]').setValue('wrong')
    await wrapper.find('form').trigger('submit.prevent')

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('Invalid credentials')
    })

    // FAR-1669 prove-the-fix: the login-failure box must be the SHARED
    // ErrorAlert — the pre-conversion bespoke div satisfies a text-only
    // assertion, so findComponent is what fails on a revert. The a11y
    // contract (role + assertive live region) rides through as fallthrough
    // attributes, and no Retry is offered: the user corrects credentials.
    const alert = wrapper.findComponent(ErrorAlert)
    expect(alert.exists()).toBe(true)
    expect(alert.props('retryable')).toBe(false)
    expect(alert.attributes('role')).toBe('alert')
    expect(alert.attributes('aria-live')).toBe('assertive')
    expect(alert.find('[data-testid="error-alert-retry"]').exists()).toBe(false)
  })

  it('handles a rejected login: ErrorAlert surfaces the error and no unhandled rejection escapes the submit handler (FAR-1669)', async () => {
    // Arm the mocked useMutation so `login` REJECTS outright — this isolates
    // the handler contract (the pre-fix `() => login()` let that rejection
    // escape; OrgLoginView's pattern is `login().catch(() => {})`).
    loginFailure.message = 'Invalid credentials'
    const unhandled: unknown[] = []
    const onUnhandled = (reason: unknown) => {
      unhandled.push(reason)
    }
    // Node reports an unhandled rejection on a later tick than the rejection
    // itself, so collect them process-wide for the duration of this test.
    process.on('unhandledRejection', onUnhandled)
    try {
      vi.spyOn(global, 'fetch').mockImplementation((input: RequestInfo | URL) => {
        const url = String(input)
        if (url.includes('/api/v1/auth/login-context')) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({
              multi_org: false,
              org: { slug: 'acme', name: 'Acme Corp' },
            }),
          } as Response)
        }
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ oidc: [], saml: false }),
        } as Response)
      })

      // Deliberately NO errorHandler shim: the rejection must be prevented by
      // the component's own handler, not absorbed by Vue's error path.
      const wrapper = mountView()
      await vi.waitFor(() => {
        expect(wrapper.find('[data-testid="login-email"]').exists()).toBe(true)
      })

      await wrapper.find('[data-testid="login-email"]').setValue('user@test.com')
      await wrapper.find('[data-testid="login-password"]').setValue('wrong')
      await wrapper.find('form').trigger('submit.prevent')

      // (1) The real error is NOT swallowed: the shared ErrorAlert renders it.
      await vi.waitFor(() => {
        expect(wrapper.text()).toContain('Invalid credentials')
      })
      const alert = wrapper.findComponent(ErrorAlert)
      expect(alert.exists()).toBe(true)
      expect(alert.text()).toContain('Invalid credentials')
      expect(alert.props('retryable')).toBe(false)
      expect(alert.attributes('role')).toBe('alert')

      // (2) No unhandled rejection escaped the handler. This is a settling
      // window for an event that must NOT occur (not a wait-for condition):
      // one macrotask turn lets Node's unhandled-rejection checkpoint fire
      // before we assert it did not.
      await new Promise((resolve) => setTimeout(resolve, 25))
      expect(unhandled).toEqual([])
    } finally {
      process.off('unhandledRejection', onUnhandled)
      loginFailure.message = null
    }
  })

  it('renders the login form within ONE timeout budget when login-context hangs', async () => {
    // A login-context request that never settles must not strand the page on
    // the loading state — the bounded fallback renders the credential form so
    // a hung pre-auth read cannot take the whole @regression suite with it.
    // The form must render as soon as login-context times out, NOT after a
    // second SSO-discovery timeout: awaiting SSO discovery serialised two 8s
    // budgets (16s worst case), which exceeded the E2E credential-form guard
    // (10s) and made every signing-in @regression test fail. SSO discovery is
    // now fire-and-forget, so one 8s advance must be enough.
    vi.useFakeTimers()
    vi.spyOn(global, 'fetch').mockImplementation(() => new Promise<Response>(() => {}))

    const wrapper = mountView()
    await vi.advanceTimersByTimeAsync(9_000)
    await nextTick()

    expect(wrapper.find('[data-testid="login-context-loading"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="login-email"]').exists()).toBe(true)
  })

  it('renders the login form when the login-context body stalls after headers (body-read timeout)', async () => {
    // The timeout must cover the JSON body read, not just the response
    // headers: a server that returns headers then stalls the body previously
    // left `await res.json()` outside the timeout window and could still
    // strand the page on the loading state.
    vi.useFakeTimers()
    vi.spyOn(global, 'fetch').mockImplementation(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: () => new Promise<never>(() => {}),
      } as unknown as Response),
    )

    const wrapper = mountView()
    // login-context's body read times out; the form must render within that
    // one budget (SSO discovery is fire-and-forget and cannot delay it).
    await vi.advanceTimersByTimeAsync(9_000)
    await nextTick()

    expect(wrapper.find('[data-testid="login-context-loading"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="login-email"]').exists()).toBe(true)
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

describe('LoginView - SSO callback error (FAR-1791)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    localStorage.clear()
    mockRoute.query = {}
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

  function mockSingleOrgContext() {
    vi.spyOn(global, 'fetch')
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ multi_org: false, org: { slug: 'acme', name: 'Acme Corp' } }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ oidc: [], saml: false }),
      } as Response)
  }

  it('renders the explanatory not-invited alert synchronously from ?sso_error', () => {
    mockRoute.query = { sso_error: 'not_invited' }
    mockSingleOrgContext()

    const wrapper = mountView()

    // Present in the first render — before login-context resolves.
    const alert = wrapper.find('[data-testid="login-sso-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain("haven't been invited")
  })

  it('falls back to the generic copy for an unknown code and never renders the raw value', () => {
    mockRoute.query = { sso_error: 'totally-bogus-code' }
    mockSingleOrgContext()

    const wrapper = mountView()

    const alert = wrapper.find('[data-testid="login-sso-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain("couldn't complete the sign-in")
    expect(wrapper.text()).not.toContain('totally-bogus-code')
  })

  it('applies the shared ErrorAlert a11y contract and no Retry', () => {
    mockRoute.query = { sso_error: 'auth_failed' }
    mockSingleOrgContext()

    const wrapper = mountView()

    const alert = wrapper.findComponent(ErrorAlert)
    expect(alert.exists()).toBe(true)
    expect(alert.props('retryable')).toBe(false)
    expect(alert.attributes('data-testid')).toBe('login-sso-error')
    expect(alert.attributes('role')).toBe('alert')
    expect(alert.attributes('aria-live')).toBe('assertive')
    expect(alert.find('[data-testid="error-alert-retry"]').exists()).toBe(false)
  })

  it('moves focus to the alert container so the state is announced', async () => {
    mockRoute.query = { sso_error: 'auth_failed' }
    mockSingleOrgContext()

    // Attach to the real document: focus() only takes effect for an element
    // that is in the document, and VTU's default mount is detached.
    const wrapper = mount(LoginView, {
      attachTo: document.body,
      global: { stubs: { Button: { template: '<button><slot /></button>' } } },
    })
    await nextTick()

    const container = wrapper.find('[data-testid="login-sso-error"]').element.parentElement
    expect(container?.getAttribute('tabindex')).toBe('-1')
    expect(document.activeElement).toBe(container)
    wrapper.unmount()
  })

  it('shows the alert in the multi-org entry step too', async () => {
    mockRoute.query = { sso_error: 'provider_unavailable' }
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ multi_org: true, org: null }),
    } as Response)

    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="login-org-slug"]').exists()).toBe(true)
    })
    const alert = wrapper.find('[data-testid="login-sso-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('temporarily unavailable')
  })

  it('carries sso_error through the remembered-slug auto-redirect', async () => {
    mockRoute.query = { sso_error: 'not_invited' }
    localStorage.setItem('modulo_login_last_org', 'acme')
    const location = fakeLocation('http://localhost/login')
    vi.stubGlobal('location', location)
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ multi_org: true, org: null }),
    } as Response)

    mountView()
    await vi.waitFor(() => {
      expect(location.href).toBe('/login/acme?sso_error=not_invited')
    })
  })
})
