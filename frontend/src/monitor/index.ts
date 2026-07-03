import type { MonitorConfig, MonitorBackend } from './types'
import { loadMonitorConfig } from './config'
import { TransportBackend } from './backends'

export { MonitorBackendRegistry } from './registry'
export type { MonitorBackend, MonitorConfig, MonitorEvent, UserInfo } from './types'
export { loadMonitorConfig }

export async function loadBackends(config: MonitorConfig): Promise<MonitorBackend[]> {
  if (!config.enabled) return []
  return [new TransportBackend()]
}
