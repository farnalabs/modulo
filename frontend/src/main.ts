import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import { createErrorTracker } from './lib/error-tracking'
import './style.css'

const app = createApp(App)
app.use(createPinia())

const errorTracker = createErrorTracker({
  appName: 'modulo',
  environment: import.meta.env.MODE === 'development' ? 'development' : 'production',
  version: import.meta.env.VITE_APP_VERSION ?? '',
})

app.use(router)
app.use(errorTracker.vuePlugin)
errorTracker.connectRouter(router)

app.mount('#app')
