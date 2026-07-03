import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import i18n from './i18n'
import { useLocaleStore } from './stores/localeStore'
import { createErrorTracker, getErrorTracker } from './lib/error-tracking'
import { loadMonitorConfig, loadBackends } from './monitor'
import { onAuthChange } from './lib/api/client'
import './style.css'

async function main() {
  const app = createApp(App)
  const pinia = createPinia()
  app.use(pinia)
  app.use(i18n)

  const monitorConfig = loadMonitorConfig()
  const backends = await loadBackends(monitorConfig)

const errorTracker = createErrorTracker({
  appName: 'modulo',
  environment: import.meta.env.MODE === 'development' ? 'development' : 'production',
  version: import.meta.env.VITE_APP_VERSION ?? '',
  monitorBackends: backends,
})
>>>>>>> 7ac4a50 (refactor: ErrorTracker uses MonitorBackendRegistry, instance methods replace module-level handlers)

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

  app.mount('#app')
}

main()
