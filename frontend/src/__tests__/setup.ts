import { markRaw } from 'vue'
import { config } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import enUS from '../locales/en-US.js'

const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: { 'en-US': enUS },
})

config.global.plugins = [...(config.global.plugins || []), i18n]

const mockRoute = {
  path: '/',
  fullPath: '/',
  params: {} as Record<string, string>,
  query: {} as Record<string, string>,
  hash: '',
  matched: [],
  name: null,
  redirectedFrom: undefined,
}

const mockRouter = {
  push: vi.fn(),
  replace: vi.fn(),
  resolve: vi.fn(),
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

config.global.stubs = {
  ...config.global.stubs,
  'router-link': { template: '<a :href="to" data-testid="router-link-stub"><slot /></a>', props: ['to'] },
  'router-view': {
    template: '<div><slot :route="route" :Component="Component" /></div>',
    data() {
      return {
        route: { fullPath: '/', path: '/', params: {}, query: {}, hash: '', matched: [], name: null, redirectedFrom: undefined, meta: {} },
        Component: markRaw({ template: '<div />' }),
      }
    },
  },
  Tooltip: { template: '<div><slot /></div>' },
  TooltipTrigger: { template: '<div><slot /></div>' },
  TooltipContent: { template: '<div><slot /></div>' },
  TooltipProvider: { template: '<div><slot /></div>' },
}
