export interface MonitorConfig {
  monitorBackends: string[]
  sentry?: { dsn: string; environment?: string; replaysSessionSampleRate?: number }
  datadogRum?: { clientToken: string }
  grafanaFaro?: { url: string }
}

export interface Breadcrumb {
  type: string
  data?: Record<string, unknown>
  timestamp?: number
}

export interface UserInfo {
  id: string
  email?: string
  name?: string
}

export interface ErrorEventInput {
  error: Error
  message?: string
  context?: Record<string, unknown>
  level?: 'error' | 'warning' | 'critical'
  user?: UserInfo
  tags?: Record<string, string>
  breadcrumbs?: Breadcrumb[]
}

export interface MonitorBackend {
  readonly key: string
  init(config: MonitorConfig): Promise<boolean>
  captureRawError(error: Error, context?: Record<string, unknown>): void
  captureMessage(message: string, level: 'error' | 'warning' | 'critical'): void
  setUser(user: UserInfo | null): void
  setTags(tags: Record<string, string>): void
  addBreadcrumb(breadcrumb: Breadcrumb): void
  dispose(): void
}
