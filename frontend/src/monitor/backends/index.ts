import type { MonitorConfig, MonitorBackend } from '../types'
import { BuiltinMonitorBackend } from './builtin'

export async function loadBackends(config: MonitorConfig): Promise<MonitorBackend[]> {
  const backends: MonitorBackend[] = []

  if (config.builtin?.enabled) {
    backends.push(new BuiltinMonitorBackend())
  }

  if (config.sentry) {
    try {
      const { SentryMonitorBackend } = await import('./sentry')
      const backend = new SentryMonitorBackend()
      if (await backend.init(config)) {
        backends.push(backend)
      }
    } catch (e) {
      console.warn('[monitor] Failed to load Sentry backend:', e)
    }
  }

  if (config['datadog-rum']) {
    try {
      const { DatadogRumMonitorBackend } = await import('./datadog-rum')
      const backend = new DatadogRumMonitorBackend()
      if (await backend.init(config)) {
        backends.push(backend)
      }
    } catch (e) {
      console.warn('[monitor] Failed to load Datadog RUM backend:', e)
    }
  }

  if (config['grafana-faro']) {
    try {
      const { GrafanaFaroMonitorBackend } = await import('./grafana-faro')
      const backend = new GrafanaFaroMonitorBackend()
      if (await backend.init(config)) {
        backends.push(backend)
      }
    } catch (e) {
      console.warn('[monitor] Failed to load Grafana Faro backend:', e)
    }
  }

  return backends
}
