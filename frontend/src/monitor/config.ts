import type { MonitorConfig } from './types'

declare global {
  interface Window {
    __MODULO_CONFIG__?: {
      monitor?: {
        monitorBackends?: string[]
        [key: string]: unknown
      }
    }
  }
}

export function loadMonitorConfig(): MonitorConfig {
  const runtimeConfig = window.__MODULO_CONFIG__?.monitor ?? {}

  const buildTimeBackends = (
    (typeof import.meta !== 'undefined' && import.meta.env?.VITE_MONITOR_BACKEND) || 'builtin'
  ).split(',').map((s: string) => s.trim())

  const activeBackends = (runtimeConfig.monitorBackends as string[]) ?? buildTimeBackends

  return {
    builtin: activeBackends.includes('builtin') ? { enabled: true } : undefined,
    sentry: activeBackends.includes('sentry') ? {
      dsn: ((runtimeConfig.sentry as any)?.dsn as string) ?? (typeof import.meta !== 'undefined' ? (import.meta.env?.VITE_SENTRY_DSN as string) : ''),
      environment: typeof import.meta !== 'undefined' ? (import.meta.env?.MODE as string) : 'production',
    } : undefined,
    'datadog-rum': activeBackends.includes('datadog-rum') ? {
      clientToken: ((runtimeConfig['datadog-rum'] as any)?.clientToken as string) ?? (typeof import.meta !== 'undefined' ? (import.meta.env?.VITE_DATADOG_RUM_CLIENT_TOKEN as string) : ''),
      site: ((runtimeConfig['datadog-rum'] as any)?.site as string) ?? 'datadoghq.com',
      service: ((runtimeConfig['datadog-rum'] as any)?.service as string) ?? 'modulo',
      env: typeof import.meta !== 'undefined' ? (import.meta.env?.MODE as string) : 'production',
    } : undefined,
    'grafana-faro': activeBackends.includes('grafana-faro') ? {
      url: ((runtimeConfig['grafana-faro'] as any)?.url as string) ?? (typeof import.meta !== 'undefined' ? (import.meta.env?.VITE_GRAFANA_FARO_URL as string) : ''),
    } : undefined,
  }
}
