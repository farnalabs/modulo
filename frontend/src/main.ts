import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import i18n from './i18n'
import { useLocaleStore } from './stores/localeStore'
import { createErrorTracker } from './lib/error-tracking'
import './style.css'

const app = createApp(App)
const pinia = createPinia()
app.use(pinia)
app.use(i18n)

const errorTracker = createErrorTracker({
  appName: 'modulo',
  environment: import.meta.env.MODE === 'development' ? 'development' : 'production',
  version: import.meta.env.VITE_APP_VERSION ?? '',
})

app.use(router)
app.use(errorTracker.vuePlugin)
errorTracker.connectRouter(router)

const localeStore = useLocaleStore()
localeStore.initLocale()

app.mount('#app')
