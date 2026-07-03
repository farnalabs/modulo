import type { MonitorConfig, MonitorBackend } from '../types'
import { BuiltinMonitorBackend } from './builtin'

type BackendConstructor = new () => MonitorBackend

async function tryLoadBackend(
  name: string,
  ctor: BackendConstructor,
  config: MonitorConfig,
): Promise<MonitorBackend | null> {
  try {
    const backend = new ctor()
    if (await backend.init(config)) {
      return backend
    }
  } catch (e) {
    console.warn(`[monitor] Failed to load ${name} backend:`, e)
  }
  return null
}

export async function loadBackends(config: MonitorConfig): Promise<MonitorBackend[]> {
  const backends: MonitorBackend[] = []

  if (config.builtin?.enabled) {
    backends.push(new BuiltinMonitorBackend())
  }

  if (config.sentry) {
    const { SentryMonitorBackend } = await import('./sentry')
    const backend = await tryLoadBackend('Sentry', SentryMonitorBackend, config)
    if (backend) backends.push(backend)
  }

  if (config['datadog-rum']) {
    const { DatadogRumMonitorBackend } = await import('./datadog-rum')
    const backend = await tryLoadBackend('Datadog RUM', DatadogRumMonitorBackend, config)
    if (backend) backends.push(backend)
  }

  if (config['grafana-faro']) {
    const { GrafanaFaroMonitorBackend } = await import('./grafana-faro')
    const backend = await tryLoadBackend('Grafana Faro', GrafanaFaroMonitorBackend, config)
    if (backend) backends.push(backend)
  }

  return backends
}
