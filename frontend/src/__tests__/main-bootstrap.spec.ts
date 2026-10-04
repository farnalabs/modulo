import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// main.ts runs `main().catch(...)` at module load. This spec proves the catch
// actually surfaces a bootstrap failure (rather than leaving an unhandled
// rejection), using the documented failure point: router.isReady() rejecting.
//
// main() itself stays REAL — the assertion depends on main.ts's catch wiring.
// Every dependency whose concern lives outside that wiring is mocked, which
// keeps the test body out of module-graph transform time (everything Vue,
// PrimeVue, the API client chain, the i18n backend, the CSS/font side-effect
// imports) and makes the spec deterministic: the mocked rejection fires within
// two microtasks of module load, so the body runs in well under a second with
// a wide margin under the 15s test timeout regardless of machine load. The
// real-router path (hook wiring + lazy route chunks) is covered by
// app-bootstrap.spec.ts, which runs after this one.

const ROUTER_MOCK = () => ({
  default: {
    install: vi.fn(),
    afterEach: vi.fn(),
    isReady: () => Promise.reject(new Error('router not ready')),
  },
})

describe('main bootstrap failure handling', () => {
  beforeEach(() => {
    vi.resetModules()

    // The documented bootstrap failure point: isReady() rejects, so main()'s
    // awaited chain rejects and must reach the .catch(console.error).
    vi.doMock('../router', ROUTER_MOCK)

    // App.vue drags the whole view graph (LoginView, AppLayout, chat
    // dependencies, useWebVitals, mustChangePassword). main() only passes the
    // component to app.component('App'), so a stub keeps the assertion intact.
    vi.doMock('../App.vue', () => ({
      default: { name: 'AppStub', render: () => null },
    }))

    // The locale/i18n instance needs message catalogs to version-hash ≥60
    // locale files at eval time; main() only app.use()s the default export.
    vi.doMock('../i18n', () => ({
      default: { install: vi.fn() },
    }))

    // Monitor backends are external SDKs (Datadog RUM, Grafana FARO,
    // Elasticsearch, Sentry). Stubbing loadBackends keeps the whole backend
    // package set out of the graph; loadMonitorConfig keeps it argv-local.
    vi.doMock('../monitor', () => ({
      loadMonitorConfig: () => ({ monitorBackends: [] }),
      loadBackends: async () => [],
    }))

    // createErrorTracker brings breadcrumbs/transport/window handlers; only
    // main()'s calls on the tracker matter here.
    vi.doMock('../lib/error-tracking', () => {
      const createErrorTracker = () => ({
        vuePlugin: { install: vi.fn() },
        connectRouter: vi.fn(),
        setUser: vi.fn(),
        setTags: vi.fn(),
      })
      return {
        createErrorTracker: vi.fn(createErrorTracker),
        getErrorTracker: vi.fn(() => null),
      }
    })

    // main() wires onAuthChange; App.vue's wider client import chain is
    // already stubbed via ../App.vue, so only this symbol is needed.
    vi.doMock('../lib/api/client', () => ({
      onAuthChange: vi.fn(),
    }))

    // CSS/font side-effect imports: style.css runs the Tailwind/postcss
    // pipeline and the fontsource packages resolve dozens of @font-face
    // rules — transform work with zero bearing on this assertion.
    vi.doMock('../style.css', () => ({}))
    vi.doMock('@fontsource-variable/inter', () => ({}))
    vi.doMock('@fontsource-variable/jetbrains-mono', () => ({}))
    vi.doMock('overlayscrollbars/styles/overlayscrollbars.css', () => ({}))

    // The Aura preset is the largest dependency in the graph (the full Aura
    // CSS-in-JS definition set) and only feeds the theme object PrimeVue is
    // given — irrelevant to whether a bootstrap rejection is caught.
    vi.doMock('@primeuix/themes/aura', () => ({ default: {} }))
    vi.doMock('primevue/config', () => ({ default: { install: vi.fn() } }))
    vi.doMock('primevue/tooltip', () => ({ default: { mounted: vi.fn(), updated: vi.fn() } }))
    vi.doMock('@tanstack/vue-query', () => ({
      VueQueryPlugin: { install: vi.fn() },
    }))

    // initLocale() runs before isReady(); a stubbed store keeps the real
    // i18n/localStorage backend-sync path out of the graph.
    vi.doMock('../stores/localeStore', () => ({
      useLocaleStore: () => ({
        initLocale: vi.fn(),
      }),
    }))
  })

  afterEach(() => {
    vi.doUnmock('../router')
    vi.doUnmock('../App.vue')
    vi.doUnmock('../i18n')
    vi.doUnmock('../monitor')
    vi.doUnmock('../lib/error-tracking')
    vi.doUnmock('../lib/api/client')
    vi.doUnmock('../style.css')
    vi.doUnmock('@fontsource-variable/inter')
    vi.doUnmock('@fontsource-variable/jetbrains-mono')
    vi.doUnmock('overlayscrollbars/styles/overlayscrollbars.css')
    vi.doUnmock('../stores/localeStore')
    vi.doUnmock('@primeuix/themes/aura')
    vi.doUnmock('primevue/config')
    vi.doUnmock('primevue/tooltip')
    vi.doUnmock('@tanstack/vue-query')
    vi.restoreAllMocks()
    vi.resetModules()
  })

  it('logs a fatal error when the application bootstrap rejects', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})

    await import('../main')

    await vi.waitFor(() => {
      expect(consoleError).toHaveBeenCalledWith(
        'Fatal: application bootstrap failed',
        expect.any(Error),
      )
    })
  })
})
