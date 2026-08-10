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
