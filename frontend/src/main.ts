import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import i18n from './i18n'
import PrimeVue from 'primevue/config'
import Tooltip from 'primevue/tooltip'
import Aura from '@primeuix/themes/aura'
import { applyPrimeVueTokenBridge } from './lib/primevue-theme'
import { useLocaleStore } from './stores/localeStore'
import { createErrorTracker } from './lib/error-tracking'
import { loadMonitorConfig, loadBackends } from './monitor'
import { VueQueryPlugin } from '@tanstack/vue-query'
import { onAuthChange } from './lib/api/client'
import './style.css'
import 'overlayscrollbars/styles/overlayscrollbars.css'

async function main() {
  // PrimeVue components consume `--p-*` design tokens; map our semantic CSS
  // variables onto them on the document root (ADR 024 Decision 4). Must run
  // once at bootstrap, after style.css has loaded (module-level import above).
  applyPrimeVueTokenBridge()

  const app = createApp(App)
  const pinia = createPinia()
  app.use(pinia)
  app.use(i18n)
  const primeuiLicense = import.meta.env.VITE_PRIMEUI_LICENSE as string | undefined
  app.use(PrimeVue, {
    ...(primeuiLicense ? { license: primeuiLicense } : {}),
    theme: {
      preset: Aura,
      options: {
        // PrimeVue's darkModeSelector selects the DARK token set. Our app is
        // dark by default (`class="dark"` on <html>) and light is toggled by
        // removing `.dark`. Simple class selectors work correctly with the
        // @primeuix/styled engine (complex selectors like `:root:not(.light)`
        // get double-wrapped, producing invalid nested CSS).
        darkModeSelector: '.dark',
      },
    },
  })
  app.directive('tooltip', Tooltip)

  const monitorConfig = loadMonitorConfig()
  const backends = await loadBackends(monitorConfig)

  const errorTracker = createErrorTracker({
    appName: 'modulo',
    environment: import.meta.env.MODE === 'development' ? 'development' : 'production',
    version: import.meta.env.VITE_APP_VERSION ?? '',
    monitorBackends: backends,
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
  // this, a direct load of a guarded route (e.g. /remy) flashes the full
  // AppLayout (incl. RemyPanel) before the auth/dev-mode guard redirects.
  await router.isReady()

  app.mount('#app')
}

main()
