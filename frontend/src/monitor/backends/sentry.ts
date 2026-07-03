import type { MonitorBackend, MonitorConfig } from '../types'

export class SentryMonitorBackend implements MonitorBackend {
  readonly key = 'sentry'
  async init(_config: MonitorConfig): Promise<boolean> {
    console.info('[monitor] Sentry backend not yet implemented — requires @sentry/vue optional dependency')
    return false
  }
}
