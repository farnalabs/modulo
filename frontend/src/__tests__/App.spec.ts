import { describe, it, expect, vi, beforeEach } from 'vitest'
import { shallowMount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import App from '../App.vue'
import { getAccessToken, getInitialAuthState } from '../lib/api/client'

const routeRef = vi.hoisted(() => ({ meta: {} as Record<string, unknown>, name: undefined as string | undefined }))
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

vi.mock('vue-router', () => ({
  useRoute: () => routeRef,
  useRouter: () => mockRouter,
  createRouter: vi.fn(() => mockRouter),
  createWebHistory: vi.fn(() => ({})),
}))

vi.mock('@/lib/api/client', () => ({
  api: { GET: vi.fn(async () => ({ data: undefined, error: undefined })) },
  getAccessToken: vi.fn(() => 'test-token'),
  setAccessToken: vi.fn(),
  setRefreshToken: vi.fn(),
  onAuthChange: vi.fn(() => vi.fn()),
  getInitialAuthState: vi.fn((hasToken: boolean) => hasToken),
  shouldReRunAutoLogin: vi.fn(() => false),
  isDemoSession: vi.fn(() => false),
  wasDemoSessionEnded: vi.fn(() => false),
}))

vi.mock('@/lib/error-tracking', () => ({
  getErrorTracker: vi.fn(() => null),
}))

vi.mock('@/config/runtime', () => ({
  getAutoLoginConfig: vi.fn(() => undefined),
}))

vi.mock('@/composables/useWebVitals', () => ({
  useWebVitals: vi.fn(),
}))

beforeEach(() => {
  setActivePinia(createPinia())
  vi.restoreAllMocks()
  routeRef.meta = {}
  routeRef.name = undefined
})

describe('App bare-route layout switch (meta.bare)', () => {
  it('renders RemyOnlyView without AppLayout when meta.bare is true', () => {
    routeRef.meta = { bare: true }
    const wrapper = shallowMount(App)
    expect(wrapper.findComponent({ name: 'RemyOnlyView' }).exists()).toBe(true)
    expect(wrapper.findComponent({ name: 'AppLayout' }).exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'LoginView' }).exists()).toBe(false)
  })

  it('renders AppLayout when meta.bare is false', () => {
    routeRef.meta = { bare: false }
    const wrapper = shallowMount(App)
    expect(wrapper.findComponent({ name: 'AppLayout' }).exists()).toBe(true)
    expect(wrapper.findComponent({ name: 'RemyOnlyView' }).exists()).toBe(false)
  })

  it('renders AppLayout when meta.bare is undefined', () => {
    routeRef.meta = {}
    const wrapper = shallowMount(App)
    expect(wrapper.findComponent({ name: 'AppLayout' }).exists()).toBe(true)
    expect(wrapper.findComponent({ name: 'RemyOnlyView' }).exists()).toBe(false)
  })
})

describe('App unauthenticated public-route rendering', () => {
  const routerViewStub = { name: 'RouterView', template: '<div data-testid="router-view-stub" />' }

  it('renders the routed view (not LoginView) for /login/:slug', () => {
    vi.mocked(getAccessToken).mockReturnValue(null)
    vi.mocked(getInitialAuthState).mockReturnValue(false)
    routeRef.name = 'org-login'

    const wrapper = shallowMount(App, { global: { stubs: { 'router-view': routerViewStub } } })

    expect(wrapper.find('[data-testid="router-view-stub"]').exists()).toBe(true)
    expect(wrapper.findComponent({ name: 'LoginView' }).exists()).toBe(false)
  })

  it('still renders LoginView directly for the bare /login route', () => {
    vi.mocked(getAccessToken).mockReturnValue(null)
    vi.mocked(getInitialAuthState).mockReturnValue(false)
    routeRef.name = 'login'

    const wrapper = shallowMount(App, { global: { stubs: { 'router-view': routerViewStub } } })

    expect(wrapper.findComponent({ name: 'LoginView' }).exists()).toBe(true)
    expect(wrapper.find('[data-testid="router-view-stub"]').exists()).toBe(false)
  })
})
