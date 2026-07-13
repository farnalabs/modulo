import type { MonitorConfig } from './types'
import type { MonitorBackend } from './types'
import { BuiltinMonitorBackend } from './backends/builtin'
import { DatadogRumMonitorBackend } from './backends/datadog-rum'
import { GrafanaFaroMonitorBackend } from './backends/grafana-faro'
import { SentryMonitorBackend } from './backends/sentry'

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

export async function loadBackends(config: MonitorConfig): Promise<MonitorBackend[]> {
  const factories: Record<string, () => MonitorBackend> = {
    builtin: () => new BuiltinMonitorBackend(),
    sentry: () => new SentryMonitorBackend(),
    'datadog-rum': () => new DatadogRumMonitorBackend(),
    datadog_rum: () => new DatadogRumMonitorBackend(),
    'grafana-faro': () => new GrafanaFaroMonitorBackend(),
    grafana_faro: () => new GrafanaFaroMonitorBackend(),
  }

  const backends: MonitorBackend[] = []
  for (const key of config.monitorBackends) {
    const create = factories[key]
    if (!create) {
      console.warn(`[monitor] Unknown backend: ${key}`)
      continue
    }
    const backend = create()
    if (await backend.init(config)) backends.push(backend)
  }
  return backends
}
