import { describe, it, expect, vi, beforeEach } from 'vitest'
import { shallowMount } from '@vue/test-utils'
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

vi.mock('vue-router', () => ({
  useRoute: () => routeRef,
  useRouter: () => mockRouter,
  createRouter: vi.fn(() => mockRouter),
  createWebHistory: vi.fn(() => ({})),
}))

vi.mock('@/lib/api/client', () => ({
  getAccessToken: vi.fn(() => 'test-token'),
  setAccessToken: vi.fn(),
  setRefreshToken: vi.fn(),
  onAuthChange: vi.fn(() => vi.fn()),
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
