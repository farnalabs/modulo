import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'

const mockRoute: Record<string, any> = {
  path: '/',
  fullPath: '/',
  params: {},
  query: {},
  hash: '',
  matched: [],
  name: null,
  redirectedFrom: undefined,
  meta: {},
}

const mockRouter = {
  push: vi.fn(),
  replace: vi.fn(),
  resolve: vi.fn((opts: { name?: string }): { path: string; meta: Record<string, string | undefined> } => {
    if (opts.name === 'dashboard') return { path: '/', meta: {} }
    if (opts.name === 'schemas') return { path: '/schemas', meta: {} }
    if (opts.name === 'library') return { path: '/library', meta: {} }
    if (opts.name === 'schema-editor') return { path: '/schemas/editor/:id?', meta: {} }
    if (opts.name === 'onboarding') return { path: '/onboarding', meta: { breadcrumb: 'Onboarding', parent: 'dashboard' } }
    return { path: '/', meta: {} }
  }),
  go: vi.fn(),
  back: vi.fn(),
  forward: vi.fn(),
  beforeEach: vi.fn(),
  afterEach: vi.fn(),
  onError: vi.fn(),
  currentRoute: { value: mockRoute },
  getRoutes: vi.fn(() => []),
  addRoute: vi.fn(),
  removeRoute: vi.fn(),
  hasRoute: vi.fn(() => false),
  isReady: vi.fn(() => Promise.resolve(true)),
}

vi.mock('vue-router', () => ({
  useRoute: vi.fn(() => mockRoute),
  useRouter: vi.fn(() => mockRouter),
  createRouter: vi.fn(() => mockRouter),
  createWebHistory: vi.fn(() => ({})),
}))

vi.mock('@/manifest.yaml', () => ({
  default: {
    routes: {
      '/': {
        name: 'dashboard',
        breadcrumb: 'Dashboard',
        parent: null,
        testid: 'page-dashboard',
        required_roles: null,
        required_tier: 'community',
        required_permissions: null,
        feature_flag: null,
      },
      '/library': {
        name: 'library',
        breadcrumb: 'Library',
        parent: '/',
        testid: 'page-library',
        required_roles: null,
        required_tier: 'community',
        required_permissions: null,
        feature_flag: null,
      },
      '/schemas': {
        name: 'schemas',
        breadcrumb: 'Schemas',
        parent: '/',
        testid: 'page-schemas',
        required_roles: null,
        required_tier: 'community',
        required_permissions: null,
        feature_flag: null,
      },
      '/schemas/editor/:id': {
        name: 'schema-editor',
        breadcrumb: 'Schema Editor',
        parent: '/schemas',
        testid: 'page-schema-editor',
        required_roles: null,
        required_tier: 'community',
        required_permissions: null,
        feature_flag: null,
      },
      '/loop-a': {
        name: 'loop-a',
        breadcrumb: 'Loop A',
        parent: '/loop-b',
        testid: 'page-loop-a',
        required_roles: null,
        required_tier: 'community',
        required_permissions: null,
        feature_flag: null,
      },
      '/loop-b': {
        name: 'loop-b',
        breadcrumb: 'Loop B',
        parent: '/loop-a',
        testid: 'page-loop-b',
        required_roles: null,
        required_tier: 'community',
        required_permissions: null,
        feature_flag: null,
      },
    },
  },
}))

beforeEach(() => {
  mockRoute.path = '/'
  mockRoute.name = null
  mockRoute.meta = {}
})

async function mountBreadcrumb() {
  const { default: AppBreadcrumb } = await import('../components/Breadcrumb.vue')
  return mount(AppBreadcrumb)
}

describe('Breadcrumb.vue', () => {
  it('does not render when route has no breadcrumb meta', async () => {
    const wrapper = await mountBreadcrumb()
    expect(wrapper.find('.breadcrumb').exists()).toBe(false)
  })

  it('renders single segment for root dashboard route', async () => {
    mockRoute.path = '/'
    mockRoute.name = 'dashboard'
    mockRoute.meta = { breadcrumb: 'Dashboard' }

    const wrapper = await mountBreadcrumb()
    const links = wrapper.findAll('.breadcrumb-link')
    const current = wrapper.find('.breadcrumb-current')

    expect(links).toHaveLength(0)
    expect(current.exists()).toBe(true)
    expect(current.text()).toBe('Dashboard')
  })

  it('renders parent chain for nested route', async () => {
    mockRoute.path = '/schemas/editor/abc'
    mockRoute.name = 'schema-editor'
    mockRoute.meta = { breadcrumb: 'Schema Editor' }

    const wrapper = await mountBreadcrumb()
    const links = wrapper.findAll('.breadcrumb-link')
    const current = wrapper.find('.breadcrumb-current')

    expect(links).toHaveLength(2)
    expect(links[0].text()).toBe('Dashboard')
    expect(links[1].text()).toBe('Schemas')
    expect(current.text()).toBe('Schema Editor')
  })

  it('renders two-level parent chain', async () => {
    mockRoute.path = '/library'
    mockRoute.name = 'library'
    mockRoute.meta = { breadcrumb: 'Library' }

    const wrapper = await mountBreadcrumb()
    const links = wrapper.findAll('.breadcrumb-link')
    const current = wrapper.find('.breadcrumb-current')

    expect(links).toHaveLength(1)
    expect(links[0].text()).toBe('Dashboard')
    expect(current.text()).toBe('Library')
  })

  it('falls back to route meta for routes not in manifest', async () => {
    mockRoute.path = '/onboarding'
    mockRoute.name = 'onboarding'
    mockRoute.meta = { breadcrumb: 'Onboarding', parent: 'dashboard' }

    mockRouter.resolve.mockImplementation((opts: { name?: string }): { path: string; meta: Record<string, string | undefined> } => {
      if (opts.name === 'dashboard') return { path: '/', meta: { breadcrumb: 'Dashboard', parent: undefined } }
      if (opts.name === 'onboarding') return { path: '/onboarding', meta: { breadcrumb: 'Onboarding', parent: 'dashboard' } }
      return { path: '/', meta: { breadcrumb: undefined, parent: undefined } }
    })

    const wrapper = await mountBreadcrumb()
    const links = wrapper.findAll('.breadcrumb-link')
    const current = wrapper.find('.breadcrumb-current')

    expect(links).toHaveLength(1)
    expect(links[0].text()).toBe('Dashboard')
    expect(current.text()).toBe('Onboarding')
  })

  it('terminates on cyclic parent chains using the visited set', async () => {
    mockRoute.path = '/loop-a'
    mockRoute.name = 'loop-a'
    mockRoute.meta = { breadcrumb: 'Loop A' }

    const wrapper = await mountBreadcrumb()
    const links = wrapper.findAll('.breadcrumb-link')
    const current = wrapper.find('.breadcrumb-current')

    expect(current.text()).toBe('Loop A')
    expect(links.map((l) => l.text())).toEqual(['Loop B'])
  })
})
