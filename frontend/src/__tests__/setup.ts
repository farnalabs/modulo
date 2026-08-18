import { markRaw, type App } from 'vue'
import { config } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import PrimeVue from 'primevue/config'
import Aura from '@primeuix/themes/aura'
import { vi } from 'vitest'
import enUS from '../locales/en-US.js'

const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: { 'en-US': enUS },
})

const isolatedVueQueryPlugin = {
  install(app: App) {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })
    app.use(VueQueryPlugin, { queryClient })
  },
}

config.global.plugins = [
  ...(config.global.plugins || []),
  i18n,
  isolatedVueQueryPlugin,
  // PrimeVue plugin so tests can mount PrimeVue components (Phase 0 / FAR-317
  // groundwork). darkModeSelector matches main.ts — dark by default, light via
  // the `html.light` class.
  [PrimeVue, { theme: { preset: Aura, options: { darkModeSelector: ':root:not(.light)' } } }],
]

// Minimal polyfills PrimeVue components need to mount in jsdom. jsdom does not
// implement ResizeObserver / IntersectionObserver, and its matchMedia is
// partially stubbed — provide no-op implementations so mounting a PrimeVue
// component in a unit test never throws.
if (typeof globalThis.ResizeObserver === 'undefined') {
  class ResizeObserverStub {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver
}

if (typeof globalThis.IntersectionObserver === 'undefined') {
  class IntersectionObserverStub {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
    takeRecords(): IntersectionObserverEntry[] {
      return []
    }
    root = null
    rootMargin = ''
    thresholds = []
  }
  globalThis.IntersectionObserver = IntersectionObserverStub as unknown as typeof IntersectionObserver
}

if (typeof globalThis.matchMedia !== 'function') {
  globalThis.matchMedia = (query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }) as MediaQueryList
}

// PrimeVue injects its generated theme <style> into the DOM on mount. jsdom's
// CSS parser cannot handle the modern CSS the Aura preset emits (e.g.
// `light-dark()`), so it emits a `jsdomError` of type `css-parsing` that jsdom
// forwards to console.error as "Could not parse CSS stylesheet". This pollutes
// tests that assert on console.error (e.g. useWebVitals.mocked.spec.ts). We
// filter those errors at the jsdom virtual-console level (below console.error),
// so the fix holds even when a test replaces console.error with its own spy.
const virtualConsole = (window as unknown as { _virtualConsole?: { _events: Record<string, unknown>; removeAllListeners: (e: string) => void; on: (e: string, fn: (...a: unknown[]) => void) => void } })._virtualConsole
if (virtualConsole) {
  const ev = virtualConsole._events['jsdomError']
  if (ev) {
    const previous: Array<(...a: unknown[]) => void> = Array.isArray(ev)
      ? (ev as unknown as Array<(...a: unknown[]) => void>)
      : [ev as (...a: unknown[]) => void]
    virtualConsole.removeAllListeners('jsdomError')
    for (const fn of previous) {
      virtualConsole.on('jsdomError', (err) => {
        const e = err as { type?: string }
        if (e && e.type === 'css-parsing') return
        fn(err)
      })
    }
  }
}

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
  install: vi.fn(),
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
