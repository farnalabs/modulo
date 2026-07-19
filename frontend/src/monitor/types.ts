export interface MonitorBackend {
  readonly key: string
  init(config: MonitorConfig): Promise<boolean>
  captureError(event: ErrorEventInput, error?: Error, context?: Record<string, unknown>): void
  captureMessage(message: string, level: MonitorLevel): void
  setUser(user: UserInfo | null): void
  setTags(tags: Record<string, string>): void
  addBreadcrumb?(breadcrumb: Breadcrumb): void
  dispose(): void
}

export interface MonitorConfig {
  monitorBackends: string[]
  sentry?: {
    dsn: string
    environment?: string
    tracesSampleRate?: number
    replaysSessionSampleRate?: number
    replaysOnErrorSampleRate?: number
  }
  datadogRum?: {
    clientToken: string
    site?: string
    service?: string
    env?: string
    version?: string
  }
  grafanaFaro?: { url: string; apiKey?: string; appName?: string }
}

export type MonitorLevel = 'error' | 'warning' | 'critical'

export interface ErrorEventInput {
  level: MonitorLevel
  message: string
  stacktrace?: string
  context_json?: Record<string, unknown>
  source?: string
  environment?: string
  version?: string
  breadcrumbs?: Breadcrumb[]
}

export interface Breadcrumb {
  type: string
  timestamp?: string
  category?: string
  message?: string
  data?: Record<string, unknown>
}

export interface UserInfo {
  id: string
  email?: string
  name?: string
  role?: string
}
