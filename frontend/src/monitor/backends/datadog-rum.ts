import type { MonitorBackend, MonitorConfig } from '../types'

export class DatadogRumMonitorBackend implements MonitorBackend {
  readonly key = 'datadog-rum'
  async init(_config: MonitorConfig): Promise<boolean> {
    console.info('[monitor] Datadog RUM backend not yet implemented — requires @datadog/browser-rum optional dependency')
    return false
  }
}
