import type { MonitorConfig } from './types'

export function loadMonitorConfig(): MonitorConfig {
  const runtime = (window as unknown as Record<string, unknown>).__MODULO_CONFIG__
  const runtimeMonitor =
    runtime && typeof runtime === 'object' && 'monitor' in runtime
      ? (runtime as Record<string, unknown>).monitor as Partial<MonitorConfig> | undefined
      : undefined

  return {
    monitorBackends: runtimeMonitor?.monitorBackends ?? (
      import.meta.env.VITE_MONITOR_BACKEND || 'builtin'
    ).split(',').map((s: string) => s.trim()).filter(Boolean),
    sentry: runtimeMonitor?.sentry ?? (
      import.meta.env.VITE_SENTRY_DSN
        ? { dsn: import.meta.env.VITE_SENTRY_DSN as string }
        : undefined
    ),
    datadogRum: runtimeMonitor?.datadogRum ?? (
      import.meta.env.VITE_DATADOG_RUM_CLIENT_TOKEN
        ? { clientToken: import.meta.env.VITE_DATADOG_RUM_CLIENT_TOKEN as string }
        : undefined
    ),
    grafanaFaro: runtimeMonitor?.grafanaFaro ?? (
      import.meta.env.VITE_GRAFANA_FARO_URL
        ? { url: import.meta.env.VITE_GRAFANA_FARO_URL as string }
        : undefined
    ),
  }
}

export async function loadBackends(config: MonitorConfig): Promise<string[]> {
  const envBackends = (import.meta.env.VITE_MONITOR_BACKEND || 'builtin')
    .split(',')
    .map((s: string) => s.trim())
    .filter(Boolean)

  if (envBackends.length > 0) {
    return envBackends
  }

  return config.monitorBackends
}
