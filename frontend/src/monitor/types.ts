import type { ErrorEventInput, Breadcrumb } from '../lib/error-tracking/types'

export type { ErrorEventInput, Breadcrumb }

export interface UserInfo {
  id: string
  email?: string
  name?: string
  role?: string
}

export interface MonitorBackend {
  readonly key: string
  init(config: MonitorConfig): boolean | Promise<boolean>
  captureError?(event: ErrorEventInput): void
  captureRawError?(error: Error, context?: Record<string, unknown>): void
  captureMessage?(message: string, level: 'error' | 'warning' | 'critical'): void
  addBreadcrumb?(breadcrumb: Breadcrumb): void
  setUser?(user: UserInfo | null): void
  setTags?(tags: Record<string, string>): void
  dispose?(): void
}

export interface MonitorConfig {
  builtin?: { enabled: boolean }
  sentry?: { dsn: string; environment?: string; replaysSessionSampleRate?: number }
  'datadog-rum'?: { clientToken: string; site?: string; service?: string; env?: string; version?: string }
  'grafana-faro'?: { url: string; apiKey?: string; appName?: string }
}
