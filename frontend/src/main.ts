import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import i18n from './i18n'
import PrimeVue from 'primevue/config'
import Tooltip from 'primevue/tooltip'
import Aura from '@primevue/themes/aura'
import { applyPrimeVueTokenBridge, installPrimeVueV4BaseFontStyles } from './lib/primevue-theme'
import { useLocaleStore } from './stores/localeStore'
import { createErrorTracker } from './lib/error-tracking'
import { loadMonitorConfig, loadBackends } from './monitor'
import { VueQueryPlugin } from '@tanstack/vue-query'
import { onAuthChange } from './lib/api/client'
import { withTimeout } from './lib/asyncUtils'
// Self-hosted fonts (FAR-1253): Modulo is self-hosted only, so the SPA must
// never call a third-party font CDN. These bundle Inter and JetBrains Mono as
// same-origin woff2 assets (font families 'Inter Variable' /
// 'JetBrains Mono Variable'), which load under the CSP's `font-src 'self'`.
import '@fontsource-variable/inter'
import '@fontsource-variable/jetbrains-mono'
import './style.css'
import 'overlayscrollbars/styles/overlayscrollbars.css'

// The pre-mount bootstrap surface lives OUTSIDE #app on purpose. The mobile
// first-paint contract (and the e2e suite that encodes it) treats "#app has
// children" as "the app has mounted": an empty #app is a blank page. A splash
// rendered into #app would make that signal true from the very first frame, so
// a stalled bootstrap would look mounted and every mount-ordering assertion
// would pass on a page that never actually mounted. Keeping the splash in its
// own body-level element preserves the signal — #app stays empty until Vue
// mounts, at which point the surface is removed.
const BOOTSTRAP_SURFACE_ID = 'modulo-bootstrap-surface'

// FAR-1892: router.isReady() has no timeout of its own. A navigation guard
// awaiting a promise that never settles, or a lazily-imported route chunk
// whose fetch never resolves, leaves it pending forever — and it used to be
// awaited before mount, so the app simply never appeared. Bound it: a slow
// initial navigation degrades to a visible app that may need a reload, never
// a permanent blank screen.
const ROUTER_READY_TIMEOUT_MS = 10_000

function bootstrapSurface(): HTMLElement {
  const existing = document.getElementById(BOOTSTRAP_SURFACE_ID)
  if (existing) return existing
  const surface = document.createElement('div')
  surface.id = BOOTSTRAP_SURFACE_ID
  document.body.appendChild(surface)
  return surface
}

function removeBootstrapSurface(): void {
  document.getElementById(BOOTSTRAP_SURFACE_ID)?.remove()
}

// Resolve a pre-mount message from the catalogue, falling back to a literal.
// This runs before (and possibly instead of) the mounted app, so it must never
// throw: an exception here would replace the bootstrap surface with an
// unhandled rejection — the exact failure this module exists to prevent.
//
// A non-empty string is NOT sufficient for it to be usable. The pinned
// vue-i18n (11.4.14) returns the KEY ITSELF when the catalogue has no entry
// for it (observed: `t('common.failed_to_load')` yields the literal string
// 'common.failed_to_load'), and it can return a whitespace-only value. Either
// would put a key-shaped or invisible string in front of the user on the fatal
// bootstrap surface — the exact moment the app has already failed to load — so
// both fall back to the literal, and the fallback stays logged so an operator
// can see it happened.
function bootstrapMessage(key: string, fallback: string): string {
  try {
    const translated = i18n.global.t(key)
    if (typeof translated === 'string' && translated !== key && translated.trim().length > 0) {
      return translated
    }
    console.warn('[bootstrap] catalogue returned no usable message; using a literal', {
      key,
      translated,
    })
    return fallback
  } catch (error) {
    console.warn('[bootstrap] i18n unavailable; using a literal message', error)
    return fallback
  }
}

// Render a pre-mount loading/error message into the bootstrap surface. The
// live-region role lives on the message element, never on #app itself, so Vue
// does not inherit it (which would make the whole application an announcement
// region) when it takes over the container.
function renderBootstrapMessage(text: string, variant: 'loading' | 'error'): void {
  const surface = bootstrapSurface()

  const message = document.createElement('div')
  message.setAttribute('role', variant === 'error' ? 'alert' : 'status')
  message.setAttribute('aria-live', variant === 'error' ? 'assertive' : 'polite')
  if (variant === 'loading') message.setAttribute('aria-busy', 'true')
  message.style.cssText = [
    'display:flex',
    'min-height:100vh',
    'align-items:center',
    'justify-content:center',
    'box-sizing:border-box',
    'padding:1.5rem',
    'text-align:center',
    'font-family:"Inter Variable",system-ui,sans-serif',
    'color:hsl(var(--foreground))',
  ].join(';')

  const paragraph = document.createElement('p')
  paragraph.textContent = text
  message.appendChild(paragraph)

  surface.replaceChildren(message)
}

function toError(value: unknown): Error {
  return value instanceof Error ? value : new Error(String(value))
}

async function main() {
  // A visible loading state before any await: if anything below stalls, the
  // user sees a message rather than a blank page (FAR-1892).
  renderBootstrapMessage(bootstrapMessage('common.loading', 'Loading…'), 'loading')

  // PrimeVue components consume `--p-*` design tokens; map our semantic CSS
  // variables onto them on the document root (ADR 024 Decision 4). Must run
  // once at bootstrap, after style.css has loaded (module-level import above).
  // The v4 base-font rule is installed alongside it: PrimeVue 4 hardcodes
  // `font-size: 1rem` on Button/InputText/Select, where v5 used a token.
  applyPrimeVueTokenBridge()
  installPrimeVueV4BaseFontStyles()

  const app = createApp(App)
  const pinia = createPinia()
  app.use(pinia)
  app.use(i18n)
  app.use(PrimeVue, {
    theme: {
      preset: Aura,
      options: {
        // PrimeVue's darkModeSelector selects the DARK token set. Our app is
        // dark by default (`class="dark"` on <html>) and light is toggled by
        // removing `.dark`. Simple class selectors work correctly with the
        // theme engine (complex selectors like `:root:not(.light)` get
        // double-wrapped, producing invalid nested CSS).
        darkModeSelector: '.dark',
      },
    },
  })
  app.directive('tooltip', Tooltip)

  // The tracker is created WITHOUT monitor backends. They are third-party SDKs
  // whose init may hang or reject, and nothing between here and the mount may
  // wait on them (FAR-1892). They are attached after the app is mounted, once
  // they have initialised.
  const errorTracker = createErrorTracker({
    appName: 'modulo',
    environment: import.meta.env.MODE === 'development' ? 'development' : 'production',
    version: import.meta.env.VITE_APP_VERSION ?? '',
  })

  app.use(router)
  app.use(errorTracker.vuePlugin)
  errorTracker.connectRouter(router)

  // Wire auth state to monitor backends
  onAuthChange((token: string | null) => {
    if (!token) {
      errorTracker.setUser(null)
      errorTracker.setTags({})
    }
    // User info is set when available via /me endpoint
  })

  const localeStore = useLocaleStore()
  localeStore.initLocale()

  // Do NOT refetch every query when the browser window regains focus: an OS
  // window switch would otherwise re-run every active query and flash loading
  // states across the whole page. Route changes and manual refreshes still
  // refetch normally; per-query `staleTime` (useDataFetch) is unaffected.
  app.use(VueQueryPlugin, {
    queryClientConfig: {
      defaultOptions: {
        queries: {
          refetchOnWindowFocus: false,
        },
      },
    },
  })

  // Mount only once the router has resolved the initial navigation. Without
  // this, a direct load of a guarded route (e.g. /assistant) flashes the full
  // AppLayout (incl. AssistantPanel) before the auth/dev-mode guard redirects.
  // Bounded, so an unresolved navigation cannot withhold the app forever.
  try {
    await withTimeout(router.isReady(), ROUTER_READY_TIMEOUT_MS, 'router.isReady')
  } catch (error) {
    console.error('Initial navigation did not complete; mounting anyway', error)
    errorTracker.captureError(toError(error), { phase: 'router.isReady' })
  }

  // Drop the bootstrap splash before Vue takes ownership of #app. Removing it
  // (rather than rendering it inside #app) is what keeps "#app has children" a
  // truthful "the app has mounted" signal for the e2e mount waits.
  removeBootstrapSurface()

  app.mount('#app')

  // Monitor backends initialise AFTER mount, so a stalled or rejecting SDK can
  // never prevent the app appearing. Failures are reported, never fatal.
  void loadBackends(loadMonitorConfig())
    .then((backends) => errorTracker.reloadBackends(backends))
    .catch((error) => {
      console.error('Monitor backend initialisation failed', error)
      errorTracker.captureError(toError(error), { phase: 'monitorBackends' })
    })
}

main().catch(error => {
  // A bootstrap failure must be visible — never left as an unhandled rejection
  // and never a blank page.
  console.error('Fatal: application bootstrap failed', error)
  renderBootstrapMessage(
    bootstrapMessage('common.failed_to_load', 'The application failed to load. Please reload the page.'),
    'error',
  )
})
