/// <reference types="vite/client" />

interface Window {
  __MODULO_CONFIG__?: {
    autoLogin?: {
      username?: unknown
      password?: unknown
    }
    monitor?: Record<string, unknown>
  }
}

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<object, object, unknown>
  export default component
}

declare module '*.yaml' {
  const data: Record<string, unknown>
  export default data
}
