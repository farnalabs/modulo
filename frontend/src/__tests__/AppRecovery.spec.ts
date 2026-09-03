import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import App from '../App.vue'

const routeRef = vi.hoisted(() => ({ meta: {} as Record<string, unknown> }))
const mockRouter = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
  go: vi.fn(),
  back: vi.fn(),
  forward: vi.fn(),
  beforeEach: vi.fn(),
  afterEach: vi.fn(),
  onError: vi.fn(),
  currentRoute: { value: routeRef },
  getRoutes: vi.fn(() => []),
  addRoute: vi.fn(),
  removeRoute: vi.fn(),
  hasRoute: vi.fn(() => false),
  isReady: vi.fn(() => Promise.resolve(true)),
}))

// Captures the auth-change handler App registers so the test can drive the
// authenticated->cleared transitions that trigger auto-login recovery.
const clientState = vi.hoisted(() => {
  let token: string | null = null
  let handler: ((t: string | null) => void) | null = null
  return {
    setToken: (t: string | null) => {
      token = t
      handler?.(t)
    },
    getToken: () => token,
    notify: (t: string | null) => handler?.(t),
    setHandler: (h: ((t: string | null) => void) | null) => {
      handler = h
    },
  }
})

// FAR-535: mirrors the persisted demo-session flag and the demo-ended
// tombstone so the App.vue mount can be driven into the "prior demo session,
// no stored token" reload states.
const demoState = vi.hoisted(() => ({ isDemo: false, demoEnded: false }))

vi.mock('vue-router', () => ({
  useRoute: () => routeRef,
  useRouter: () => mockRouter,
  createRouter: vi.fn(() => mockRouter),
  createWebHistory: vi.fn(() => ({})),
}))

vi.mock('@/lib/api/client', () => ({
  getAccessToken: vi.fn(() => clientState.getToken()),
  setAccessToken: vi.fn((t: string) => clientState.setToken(t)),
  setRefreshToken: vi.fn(),
  onAuthChange: vi.fn((fn: (t: string | null) => void) => {
    clientState.setHandler(fn)
    fn(clientState.getToken())
    return () => clientState.setHandler(null)
  }),
  getInitialAuthState: vi.fn((hasToken: boolean) => hasToken),
  shouldReRunAutoLogin: vi.fn(
    (wasAuthenticated: boolean, hasToken: boolean, hasAutoLogin: boolean) =>
      wasAuthenticated && !hasToken && hasAutoLogin,
  ),
  isDemoSession: vi.fn(() => demoState.isDemo),
  wasDemoSessionEnded: vi.fn(() => demoState.demoEnded),
}))

vi.mock('@/lib/error-tracking', () => ({
  getErrorTracker: vi.fn(() => null),
}))

vi.mock('@/config/runtime', () => ({
  getAutoLoginConfig: vi.fn(() => ({ username: 'demo@modulo', password: 'demo' })),
}))

vi.mock('@/composables/useWebVitals', () => ({
  useWebVitals: vi.fn(),
}))

// Deferred fetch mock so a recovery login can be held in flight while a
// concurrent 401 notification is driven through onAuthChange.
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.restoreAllMocks()
  clientState.setToken(null)
  clientState.setHandler(null)
  demoState.isDemo = false
  demoState.demoEnded = false
  mockRouter.push.mockClear()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function mockLoginFetch(deferredLogin: ReturnType<typeof deferred<{ ok: boolean; json: () => Promise<Record<string, unknown>> }>>) {
  const fetchMock = vi.fn((input: string) => {
    if (String(input).includes('/api/v1/auth/login')) {
      return deferredLogin.promise
    }
    return Promise.resolve({ ok: true, json: async () => ({}) })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('App auto-login recovery', () => {
  it('keeps the app rendered when concurrent 401s clear the token during recovery', async () => {
    // Start with an existing (soon-to-expire) stored token.
    clientState.setToken('stale-token')
    const deferredLogin = deferred<{ ok: boolean; json: () => Promise<Record<string, unknown>> }>()
    mockLoginFetch(deferredLogin)

    const wrapper = shallowMount(App)
    expect(wrapper.findComponent({ name: 'LoginView' }).exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'AppLayout' }).exists()).toBe(true)

    // First 401 -> refresh failure -> clearAccessToken -> onAuthChange(null).
    clientState.notify(null)
    // Recovery is now in flight (login fetch pending, app held rendered).
    expect(wrapper.findComponent({ name: 'LoginView' }).exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'AppLayout' }).exists()).toBe(true)

    // A concurrent 401 clears the token again while recovery is in flight.
    clientState.notify(null)
    await flushPromises()
    // Must NOT flash LoginView mid-recovery.
    expect(wrapper.findComponent({ name: 'LoginView' }).exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'AppLayout' }).exists()).toBe(true)

    // Recovery login succeeds -> app stays rendered, no forced navigation.
    deferredLogin.resolve({ ok: true, json: async () => ({ access_token: 'fresh-token', refresh_token: 'fresh-refresh', user: { id: '1', email: 'demo@modulo', name: 'Demo' } }) })
    await flushPromises()
    expect(wrapper.findComponent({ name: 'LoginView' }).exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'AppLayout' }).exists()).toBe(true)
    expect(mockRouter.push).not.toHaveBeenCalledWith('/')
  })

  it('shows LoginView only when the recovery login also fails', async () => {
    clientState.setToken('stale-token')
    const deferredLogin = deferred<{ ok: boolean; json: () => Promise<Record<string, unknown>> }>()
    mockLoginFetch(deferredLogin)

    const wrapper = shallowMount(App)
    clientState.notify(null)
    await flushPromises()

    deferredLogin.resolve({ ok: false, json: async () => ({ detail: 'bad credentials' }) })
    await flushPromises()
    expect(wrapper.findComponent({ name: 'LoginView' }).exists()).toBe(true)
    expect(wrapper.findComponent({ name: 'AppLayout' }).exists()).toBe(false)
  })

  it('does not navigate to the dashboard root on recovery success', async () => {
    clientState.setToken('stale-token')
    const deferredLogin = deferred<{ ok: boolean; json: () => Promise<Record<string, unknown>> }>()
    mockLoginFetch(deferredLogin)

    shallowMount(App)
    clientState.notify(null)
    deferredLogin.resolve({ ok: true, json: async () => ({ access_token: 'fresh-token', refresh_token: 'fresh-refresh', user: { id: '1', email: 'demo@modulo', name: 'Demo' } }) })
    await flushPromises()

    // Recovery is a mid-session re-auth: the user stays on their deep link.
    expect(mockRouter.push).not.toHaveBeenCalled()
  })

  it('navigates to the dashboard root after first-mount auto-login succeeds', async () => {
    // No stored token -> fresh mount runs auto-login with navigation.
    const deferredLogin = deferred<{ ok: boolean; json: () => Promise<Record<string, unknown>> }>()
    mockLoginFetch(deferredLogin)

    const wrapper = shallowMount(App)
    await flushPromises()

    deferredLogin.resolve({ ok: true, json: async () => ({ access_token: 'fresh-token', refresh_token: 'fresh-refresh', user: { id: '1', email: 'demo@modulo', name: 'Demo' } }) })
    await flushPromises()

    expect(mockRouter.push).toHaveBeenCalledWith('/')
    expect(wrapper.findComponent({ name: 'LoginView' }).exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'AppLayout' }).exists()).toBe(true)
  })
})

// FAR-535: on reload with a prior demo session but no stored token (the
// short-lived demo session has ended), App must NOT silently auto-login as the
// instance's auto-login account — it hands back to the /demo route, whose guard
// re-issues a fresh demo session (or lands on /login when demo is disabled).
// qa iter 1: the signal is the persisted demo-ended tombstone (which survives
// clearAccessToken + reload); the live demo-marker check stays as a second
// signal for the mid-session path.
describe('App demo reload guard (FAR-535)', () => {
  it('redirects to /demo and never calls auto-login when the demo flag is set without a token', async () => {
    demoState.isDemo = true
    const fetchMock = vi.fn(() => Promise.resolve({ ok: true, json: async () => ({}) }))
    vi.stubGlobal('fetch', fetchMock)

    const wrapper = shallowMount(App)
    await flushPromises()

    expect(mockRouter.push).toHaveBeenCalledWith('/demo')
    // The instance auto-login credential POST must never fire.
    expect(fetchMock).not.toHaveBeenCalledWith(
      '/api/v1/auth/login',
      expect.anything(),
    )
    // No session was established, so the unauthenticated shell stays rendered
    // until the /demo guard's hand-off resolves.
    expect(wrapper.findComponent({ name: 'LoginView' }).exists()).toBe(true)
    expect(wrapper.findComponent({ name: 'AppLayout' }).exists()).toBe(false)
  })

  it('redirects to /demo and never calls auto-login when the demo-ended tombstone is present without a token', async () => {
    // qa iter 1: the tombstone is the signal that actually fires on the real
    // expiry path — clearAccessToken removes token AND demo marker together,
    // so after a reload only the tombstone remembers the demo session.
    demoState.demoEnded = true
    const fetchMock = vi.fn(() => Promise.resolve({ ok: true, json: async () => ({}) }))
    vi.stubGlobal('fetch', fetchMock)

    const wrapper = shallowMount(App)
    await flushPromises()

    expect(mockRouter.push).toHaveBeenCalledWith('/demo')
    expect(fetchMock).not.toHaveBeenCalledWith(
      '/api/v1/auth/login',
      expect.anything(),
    )
    expect(wrapper.findComponent({ name: 'LoginView' }).exists()).toBe(true)
    expect(wrapper.findComponent({ name: 'AppLayout' }).exists()).toBe(false)
  })

  it('keeps first-mount auto-login unchanged when no demo signal is present', async () => {
    // A real login (or any successful auth) clears the tombstone and the demo
    // marker (see lib/api/auth.ts), so a genuine fresh visitor auto-logs-in
    // exactly as before.
    const deferredLogin = deferred<{ ok: boolean; json: () => Promise<Record<string, unknown>> }>()
    mockLoginFetch(deferredLogin)

    shallowMount(App)
    await flushPromises()

    // Auto-login ran (the login POST is in flight) and no /demo hand-off was requested.
    expect(mockRouter.push).not.toHaveBeenCalledWith('/demo')
    deferredLogin.resolve({ ok: true, json: async () => ({ access_token: 'fresh-token', refresh_token: 'fresh-refresh', user: { id: '1', email: 'demo@modulo', name: 'Demo' } }) })
    await flushPromises()
    expect(mockRouter.push).toHaveBeenCalledWith('/')
  })

  it('takes the normal (auto-)login flow — never /demo — after an explicit demo logout (qa iter 2)', async () => {
    // AppLayout.logout clears with { demoEnded: false }: after the reload
    // neither the demo marker nor the demo-ended tombstone exists, so App must
    // behave exactly as for a fresh visitor. With auto-login configured that
    // means the credential POST fires and lands on '/'; with no auto-login it
    // would be the plain /login view. It must NEVER push back to /demo — that
    // would re-mint a demo session the visitor explicitly walked away from.
    const deferredLogin = deferred<{ ok: boolean; json: () => Promise<Record<string, unknown>> }>()
    const fetchMock = mockLoginFetch(deferredLogin)

    shallowMount(App)
    await flushPromises()

    expect(mockRouter.push).not.toHaveBeenCalledWith('/demo')
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/auth/login', expect.anything())
    deferredLogin.resolve({ ok: true, json: async () => ({ access_token: 'fresh-token', refresh_token: 'fresh-refresh', user: { id: '1', email: 'demo@modulo', name: 'Demo' } }) })
    await flushPromises()
    expect(mockRouter.push).toHaveBeenCalledWith('/')
  })
})
