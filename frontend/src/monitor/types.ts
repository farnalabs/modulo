import type { ErrorEventInput, Breadcrumb } from '../lib/error-tracking/types'

export type { ErrorEventInput, Breadcrumb }

export interface MonitorBackend {
  readonly key: string
  init(config: MonitorConfig): Promise<boolean>
  captureError(event: ErrorEventInput): void
  captureMessage(message: string, level: 'error' | 'warning' | 'critical'): void
  setUser(user: UserInfo | null): void
  setTags(tags: Record<string, string>): void
  dispose(): void
}

export interface UserInfo {
  id: string
  name?: string
  email?: string
}

export interface DatadogRumConfig {
  clientToken: string
  site?: string
  service?: string
  env?: string
  version?: string
}

export interface MonitorConfig {
  monitorBackends: string[]
  sentry?: { dsn: string }
  datadogRum?: { clientToken: string }
  'datadog-rum'?: DatadogRumConfig
  grafanaFaro?: { url: string }
}
