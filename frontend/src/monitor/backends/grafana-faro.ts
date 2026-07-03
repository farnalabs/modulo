import type { MonitorBackend, MonitorConfig } from '../types'

export class GrafanaFaroMonitorBackend implements MonitorBackend {
  readonly key = 'grafana-faro'
  async init(_config: MonitorConfig): Promise<boolean> {
    console.info('[monitor] Grafana Faro backend not yet implemented — requires @grafana/faro-web-sdk optional dependency')
    return false
  }
}
