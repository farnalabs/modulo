import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// main.ts runs `main().catch(...)` at module load. This spec proves the whole
// bootstrap contract around that wiring:
//   - a stalled or rejecting monitor backend can no longer withhold the app
//     (they initialise after mount now, never on the mount path);
//   - a rejected initial navigation still mounts the app;
//   - a genuinely fatal bootstrap failure is LOGGED *and* drawn into the
//     bootstrap surface (a sibling of #app, so it never makes #app look
//     mounted), so the user never faces an unexplained blank page.
//
// main() itself stays REAL — every assertion depends on main.ts's own wiring.
// Only the dependencies whose concern lives outside that wiring are mocked,
// which keeps the test body out of module-graph transform time (everything
// Vue, PrimeVue, the API client chain, the i18n backend, the CSS/font
// side-effect imports). The component libraries are real: `createApp`/`mount`
// are the behaviour under test. The real-router path (hook wiring + lazy route
// chunks) is covered by app-bootstrap.spec.ts, which runs after this one.

const { loadBackendsMock, isReadyMock, initLocaleMock, captureErrorMock } = vi.hoisted(() => ({
  loadBackendsMock: vi.fn(),
  isReadyMock: vi.fn(),
  initLocaleMock: vi.fn(),
  captureErrorMock: vi.fn(),
}))

const APP_MARKER = '[data-testid="app-mounted"]'

// The default composer shape: `global.t` mirrors the real composer's shape so
// the bootstrap message path is exercisable without the catalogue.
const CATALOGUE_COMPOSER = { t: (key: string) => `t:${key}` }

// Register the i18n mock for THIS test and prove the module graph will resolve
// it. `main.ts` imports `../i18n` transitively, and a previously-registered mock
// (from the shared `beforeEach`, or a prior test in the file) can otherwise win
// that transitive import — so the literal-fallback cases silently received the
// catalogue mock's `t:common.failed_to_load` instead of exercising their own
// composer (FAR-1943). Re-resolving `../i18n` here after re-registering pins the
// intended mock, and the assertion fails at the mock rather than mysteriously at
// the rendered message if a future change reintroduces the leak.
async function installI18nMock(composer?: { t: (key: string) => string }): Promise<void> {
  vi.doUnmock('../i18n')
  vi.resetModules()
  vi.doMock('../i18n', () => ({
    default: {
      install: vi.fn(),
      ...(composer ? { global: composer } : {}),
    },
  }))

  const resolved = (await import('../i18n')).default as {
    global?: { t: (key: string) => string }
  }
  if (composer) {
    expect(resolved.global?.t('probe')).toBe(composer.t('probe'))
  } else {
    expect(resolved.global).toBeUndefined()
  }
}

describe('main bootstrap', () => {
  beforeEach(() => {
    vi.resetModules()

    document.body.innerHTML = '<div id="app"></div>'

    // Per-test defaults; each test overrides the one behaviour it exercises.
    loadBackendsMock.mockReset()
    loadBackendsMock.mockResolvedValue([])
    isReadyMock.mockReset()
    isReadyMock.mockResolvedValue(undefined)
    initLocaleMock.mockReset()
    captureErrorMock.mockReset()

    // The router resolves the initial navigation in every test except the one
    // that pins a rejected navigation.
    vi.doMock('../router', () => ({
      default: {
        install: vi.fn(),
        afterEach: vi.fn(),
        isReady: isReadyMock,
      },
    }))

    // App.vue drags the whole view graph (LoginView, AppLayout, chat
    // dependencies, useWebVitals, mustChangePassword). The stub renders a
    // marker so "did the app mount?" is answered from the DOM.
    vi.doMock('../App.vue', async () => {
      const { h } = await vi.importActual<typeof import('vue')>('vue')
      return {
        default: {
          name: 'AppStub',
          render: () => h('div', { 'data-testid': 'app-mounted' }),
        },
      }
    })

    // The locale/i18n instance (see installI18nMock) needs message catalogs to
    // version-hash ≥60 locale files at eval time; each test installs its own
    // composer mock so the module graph never inherits a previous test's.

    // Monitor backends are external SDKs (Datadog RUM, Grafana FARO, Sentry).
    // Stubbing loadBackends keeps the whole backend package set out of the
    // graph; loadMonitorConfig keeps it argv-local.
    vi.doMock('../monitor', () => ({
      loadMonitorConfig: () => ({ monitorBackends: [] }),
      loadBackends: loadBackendsMock,
    }))

    // createErrorTracker brings breadcrumbs/transport/window handlers; only
    // main()'s calls on the tracker matter here.
    vi.doMock('../lib/error-tracking', () => ({
      createErrorTracker: vi.fn(() => ({
        vuePlugin: { install: vi.fn() },
        connectRouter: vi.fn(),
        setUser: vi.fn(),
        setTags: vi.fn(),
        captureError: captureErrorMock,
        reloadBackends: vi.fn(),
      })),
      getErrorTracker: vi.fn(() => null),
    }))

    // main() wires onAuthChange; App.vue's wider client import chain is
    // already stubbed via ../App.vue, so only this symbol is needed.
    vi.doMock('../lib/api/client', () => ({
      onAuthChange: vi.fn(),
    }))

    // CSS/font side-effect imports: style.css runs the Tailwind/postcss
    // pipeline and the fontsource packages resolve dozens of @font-face
    // rules — transform work with zero bearing on these assertions.
    vi.doMock('../style.css', () => ({}))
    vi.doMock('@fontsource-variable/inter', () => ({}))
    vi.doMock('@fontsource-variable/jetbrains-mono', () => ({}))
    vi.doMock('overlayscrollbars/styles/overlayscrollbars.css', () => ({}))

    // The Aura preset is the largest dependency in the graph (the full Aura
    // CSS-in-JS definition set) and only feeds the theme object PrimeVue is
    // given — irrelevant to the bootstrap ordering under test.
    vi.doMock('@primevue/themes/aura', () => ({ default: {} }))
    vi.doMock('primevue/config', () => ({ default: { install: vi.fn() } }))
    vi.doMock('primevue/tooltip', () => ({ default: { mounted: vi.fn(), updated: vi.fn() } }))
    vi.doMock('@tanstack/vue-query', () => ({
      VueQueryPlugin: { install: vi.fn() },
    }))

    // initLocale() runs before mount; a stubbed store keeps the real
    // i18n/localStorage backend-sync path out of the graph.
    vi.doMock('../stores/localeStore', () => ({
      useLocaleStore: () => ({
        initLocale: initLocaleMock,
      }),
    }))
  })

  afterEach(() => {
    document.body.innerHTML = ''
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
    vi.doUnmock('@primevue/themes/aura')
    vi.doUnmock('primevue/config')
    vi.doUnmock('primevue/tooltip')
    vi.doUnmock('@tanstack/vue-query')
    vi.restoreAllMocks()
    vi.resetModules()
  })

  it('mounts even when a monitor backend init never settles', async () => {
    await installI18nMock(CATALOGUE_COMPOSER)
    // A backend SDK whose chunk fetch never settles: loadBackends() never
    // resolves. Nothing on the mount path may await it (FAR-1892).
    loadBackendsMock.mockReturnValue(new Promise(() => {}))

    await import('../main')

    await vi.waitFor(() => {
      expect(document.querySelector(APP_MARKER)).not.toBeNull()
    })
    // The pre-mount splash lives OUTSIDE #app (so "#app has children" stays a
    // truthful mount signal) and must be removed once Vue takes ownership.
    expect(document.querySelector('#app [role="status"]')).toBeNull()
    expect(document.getElementById('modulo-bootstrap-surface')).toBeNull()
  })

  it('mounts and reports it when monitor backend initialisation rejects', async () => {
    await installI18nMock(CATALOGUE_COMPOSER)
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    loadBackendsMock.mockRejectedValue(new Error('sdk blew up'))

    await import('../main')

    await vi.waitFor(() => {
      expect(consoleError).toHaveBeenCalledWith(
        'Monitor backend initialisation failed',
        expect.any(Error),
      )
    })
    expect(document.querySelector(APP_MARKER)).not.toBeNull()
    expect(captureErrorMock).toHaveBeenCalledWith(expect.any(Error), { phase: 'monitorBackends' })
  })

  it('mounts when the initial navigation rejects', async () => {
    await installI18nMock(CATALOGUE_COMPOSER)
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    isReadyMock.mockRejectedValue(new Error('router not ready'))

    await import('../main')

    await vi.waitFor(() => {
      expect(consoleError).toHaveBeenCalledWith(
        'Initial navigation did not complete; mounting anyway',
        expect.any(Error),
      )
    })
    expect(document.querySelector(APP_MARKER)).not.toBeNull()
    expect(captureErrorMock).toHaveBeenCalledWith(expect.any(Error), { phase: 'router.isReady' })
  })

  it('wraps a non-Error navigation rejection into an Error for the tracker', async () => {
    await installI18nMock(CATALOGUE_COMPOSER)
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    // A guard can reject with a non-Error (a string, an API error body). The
    // tracker's contract takes an Error, so toError must convert it.
    isReadyMock.mockRejectedValue('navigation stalled')

    await import('../main')

    await vi.waitFor(() => {
      expect(captureErrorMock).toHaveBeenCalled()
    })
    const [wrapped] = captureErrorMock.mock.calls[0]
    expect(wrapped).toBeInstanceOf(Error)
    expect((wrapped as Error).message).toBe('navigation stalled')
    expect(captureErrorMock).toHaveBeenCalledWith(expect.any(Error), { phase: 'router.isReady' })
    expect(consoleError).toHaveBeenCalledWith(
      'Initial navigation did not complete; mounting anyway',
      'navigation stalled',
    )
  })

  it('logs a fatal error when the application bootstrap rejects', async () => {
    await installI18nMock(CATALOGUE_COMPOSER)
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    initLocaleMock.mockImplementation(() => {
      throw new Error('locale store exploded')
    })

    await import('../main')

    await vi.waitFor(() => {
      expect(consoleError).toHaveBeenCalledWith(
        'Fatal: application bootstrap failed',
        expect.any(Error),
      )
    })
  })

  it('draws a fatal bootstrap failure into the DOM instead of leaving a blank page', async () => {
    await installI18nMock(CATALOGUE_COMPOSER)
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    initLocaleMock.mockImplementation(() => {
      throw new Error('locale store exploded')
    })

    await import('../main')

    await vi.waitFor(() => {
      expect(consoleError).toHaveBeenCalledWith(
        'Fatal: application bootstrap failed',
        expect.any(Error),
      )
    })

    const surface = document.querySelector('#modulo-bootstrap-surface [role="alert"]')
    expect(surface).not.toBeNull()
    expect(surface?.textContent).toBe('t:common.failed_to_load')
    // The failure is surfaced instead of the half-built app.
    expect(document.querySelector(APP_MARKER)).toBeNull()
  })

  it('falls back to a literal message when the fatal path cannot translate', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    const consoleWarn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    // An i18n plugin with no usable composer: resolving either message throws.
    await installI18nMock()
    initLocaleMock.mockImplementation(() => {
      throw new Error('locale store exploded')
    })

    await import('../main')

    await vi.waitFor(() => {
      expect(consoleError).toHaveBeenCalledWith(
        'Fatal: application bootstrap failed',
        expect.any(Error),
      )
    })

    const surface = document.querySelector('#modulo-bootstrap-surface [role="alert"]')
    expect(surface).not.toBeNull()
    expect(surface?.textContent).toBe('The application failed to load. Please reload the page.')
    expect(consoleWarn).toHaveBeenCalledWith(
      '[bootstrap] i18n unavailable; using a literal message',
      expect.any(Error),
    )
  })

  it('falls back to a literal message when the catalogue resolves an empty string', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    // A key can resolve to an empty string (missing/blank catalogue entry);
    // an empty message would render an invisible surface, so the literal wins.
    await installI18nMock({ t: () => '' })
    initLocaleMock.mockImplementation(() => {
      throw new Error('locale store exploded')
    })

    await import('../main')

    await vi.waitFor(() => {
      expect(document.querySelector('#modulo-bootstrap-surface [role="alert"]')).not.toBeNull()
    })

    const surface = document.querySelector('#modulo-bootstrap-surface [role="alert"]')
    expect(surface?.textContent).toBe('The application failed to load. Please reload the page.')
    expect(consoleError).toHaveBeenCalledWith(
      'Fatal: application bootstrap failed',
      expect.any(Error),
    )
  })

  it('falls back to the literal when the catalogue returns the key verbatim', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    const consoleWarn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    // The pinned vue-i18n (11.4.14) returns the KEY ITSELF when the catalogue
    // has no entry for it — observed against a real createI18n instance:
    // `t('common.failed_to_load') === 'common.failed_to_load'` (alongside an
    // `[intlify] Not found ...` warning). A key-shaped string must never reach
    // the fatal surface, so this composer reproduces that verbatim return.
    await installI18nMock({ t: (key: string) => key })
    initLocaleMock.mockImplementation(() => {
      throw new Error('locale store exploded')
    })

    await import('../main')

    await vi.waitFor(() => {
      expect(document.querySelector('#modulo-bootstrap-surface [role="alert"]')).not.toBeNull()
    })

    const surface = document.querySelector('#modulo-bootstrap-surface [role="alert"]')
    expect(surface?.textContent).toBe('The application failed to load. Please reload the page.')
    expect(consoleWarn).toHaveBeenCalledWith(
      '[bootstrap] catalogue returned no usable message; using a literal',
      expect.objectContaining({ key: 'common.failed_to_load', translated: 'common.failed_to_load' }),
    )
    expect(consoleError).toHaveBeenCalledWith(
      'Fatal: application bootstrap failed',
      expect.any(Error),
    )
  })

  it('falls back to the literal when the catalogue resolves only whitespace', async () => {
    const consoleWarn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    // vue-i18n can return a whitespace-only catalogue value verbatim (observed:
    // `'   '`). Rendered, it is an invisible surface — not a usable message.
    await installI18nMock({ t: () => '   ' })
    initLocaleMock.mockImplementation(() => {
      throw new Error('locale store exploded')
    })

    await import('../main')

    await vi.waitFor(() => {
      expect(document.querySelector('#modulo-bootstrap-surface [role="alert"]')).not.toBeNull()
    })

    const surface = document.querySelector('#modulo-bootstrap-surface [role="alert"]')
    expect(surface?.textContent).toBe('The application failed to load. Please reload the page.')
    expect(consoleWarn).toHaveBeenCalledWith(
      '[bootstrap] catalogue returned no usable message; using a literal',
      expect.objectContaining({ key: 'common.failed_to_load', translated: '   ' }),
    )
  })
})
