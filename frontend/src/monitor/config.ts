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

function envVar(key: string): string | undefined {
  if (typeof import.meta === 'undefined') return undefined
  return (import.meta.env as Record<string, string | undefined>)[key]
}

function runtimeValue<T>(config: Record<string, unknown>, key: string): T | undefined {
  return config[key] as T | undefined
}

export function loadMonitorConfig(): MonitorConfig {
  const runtimeConfig = (window.__MODULO_CONFIG__?.monitor ?? {}) as Record<string, unknown>

  const buildTimeBackends = (envVar('VITE_MONITOR_BACKEND') || 'builtin')
    .split(',').map((s: string) => s.trim())

  const activeBackends = (runtimeConfig.monitorBackends as string[]) ?? buildTimeBackends

  return {
    builtin: activeBackends.includes('builtin') ? { enabled: true } : undefined,
    sentry: activeBackends.includes('sentry') ? {
      dsn: runtimeValue<{ dsn?: string }>(runtimeConfig, 'sentry')?.dsn ?? envVar('VITE_SENTRY_DSN') ?? '',
      environment: envVar('MODE') ?? 'production',
    } : undefined,
    'datadog-rum': activeBackends.includes('datadog-rum') ? {
      clientToken: runtimeValue<{ clientToken?: string }>(runtimeConfig, 'datadog-rum')?.clientToken ?? envVar('VITE_DATADOG_RUM_CLIENT_TOKEN') ?? '',
      site: runtimeValue<{ site?: string }>(runtimeConfig, 'datadog-rum')?.site ?? 'datadoghq.com',
      service: runtimeValue<{ service?: string }>(runtimeConfig, 'datadog-rum')?.service ?? 'modulo',
      env: envVar('MODE') ?? 'production',
    } : undefined,
    'grafana-faro': activeBackends.includes('grafana-faro') ? {
      url: runtimeValue<{ url?: string }>(runtimeConfig, 'grafana-faro')?.url ?? envVar('VITE_GRAFANA_FARO_URL') ?? '',
    } : undefined,
  }
}
