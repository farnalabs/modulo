import type { MonitorConfig, MonitorBackend } from './types'
import { withTimeout } from '../lib/asyncUtils'
import { BuiltinMonitorBackend } from './backends/builtin'
import { DatadogRumMonitorBackend } from './backends/datadog-rum'
import { GrafanaFaroMonitorBackend } from './backends/grafana-faro'
import { SentryMonitorBackend } from './backends/sentry'

// Each backend's `init()` pulls in its SDK over the network, so it can hang
// indefinitely (a stalled chunk fetch). The loop is sequential, so one stuck
// init would starve every backend registered after it — bound each init and
// drop only the backend that did not come up in time (FAR-1892).
const MONITOR_BACKEND_INIT_TIMEOUT_MS = 5_000

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

/**
 * Initialise every configured backend and return the ones that came up.
 *
 * `options.initTimeoutMs` overrides the per-backend init bound (meant for
 * callers and tests that need a tighter bound than the default).
 */
export async function loadBackends(
  config: MonitorConfig,
  options: { initTimeoutMs?: number } = {},
): Promise<MonitorBackend[]> {
  const initTimeoutMs = options.initTimeoutMs ?? MONITOR_BACKEND_INIT_TIMEOUT_MS
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
    try {
      if (await withTimeout(backend.init(config), initTimeoutMs, `${backend.key}.init`)) {
        backends.push(backend)
      }
    } catch (error) {
      // Fail open WITH a log: a monitoring backend is best-effort and must
      // never take down the app or the other backends.
      console.warn(`[monitor] Backend ${backend.key} init failed; continuing without it`, error)
    }
  }
  return backends
}
