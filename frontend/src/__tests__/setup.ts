import { markRaw, type App } from 'vue'
import { config } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import PrimeVue from 'primevue/config'
import Aura from '@primeuix/themes/aura'
import Tooltip from 'primevue/tooltip'
import { vi } from 'vitest'
import enUS from '../locales/en-US.js'

// jsdom does not implement CSS.escape (jsdom#1555), which the UI-command
// executor and other interactable-introspection code use to build attribute
// selectors. Provide a spec-compliant polyfill (port of mathiasbynens/css.escape)
// so those code paths are unit-testable.
if (typeof globalThis.CSS === 'undefined') {
  ;(globalThis as Record<string, unknown>).CSS = {}
}
if (typeof (globalThis.CSS as { escape?: unknown }).escape !== 'function') {
  ;(globalThis.CSS as Record<string, unknown>).escape = (value: string): string => {
    const string = String(value)
    const length = string.length
    const firstCodeUnit = string.charCodeAt(0)
    let result = ''
    for (let index = -1; ++index < length;) {
      const codeUnit = string.charCodeAt(index)
      if (codeUnit === 0x0000) {
        result += '\uFFFD'
        continue
      }
      if (
        (codeUnit >= 0x0001 && codeUnit <= 0x001f) ||
        codeUnit >= 0x007f ||
        (index === 0 && codeUnit >= 0x0030 && codeUnit <= 0x0039) ||
        (index === 1 && codeUnit >= 0x0030 && codeUnit <= 0x0039 && firstCodeUnit === 0x002d)
      ) {
        result += `\\${codeUnit.toString(16)} `
        continue
      }
      if (index === 0 && length === 1 && codeUnit === 0x002d) {
        result += `\\${string.charAt(index)}`
        continue
      }
      if (
        codeUnit >= 0x0080 ||
        codeUnit === 0x002d ||
        codeUnit === 0x005f ||
        (codeUnit >= 0x0030 && codeUnit <= 0x0039) ||
        (codeUnit >= 0x0041 && codeUnit <= 0x005a) ||
        (codeUnit >= 0x0061 && codeUnit <= 0x007a)
      ) {
        result += string.charAt(index)
        continue
      }
      result += `\\${string.charAt(index)}`
    }
    return result
  }
}

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
  // groundwork). darkModeSelector matches main.ts — dark by default
  // (`class="dark"` on <html>), light by adding `.light` and removing `.dark`.
  [PrimeVue, { theme: { preset: Aura, options: { darkModeSelector: '.dark' } } }],
]

config.global.directives = {
  ...config.global.directives,
  tooltip: Tooltip,
}

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
  }
