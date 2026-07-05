export interface MonitorBackend {
  key: string
  init(config: MonitorConfig): Promise<boolean>
  captureError(event: MonitorEvent, error?: Error, context?: Record<string, unknown>): void
  captureMessage(message: string, level: string): void
  setUser(user: UserInfo | null): void
  setTags(tags: Record<string, string>): void
  addBreadcrumb?(breadcrumb: Breadcrumb): void
  dispose(): void
}

export interface MonitorConfig {
  monitorBackends: string[]
  sentry?: { dsn: string }
  datadogRum?: { clientToken: string }
  grafanaFaro?: { url: string; apiKey?: string; appName?: string }
}

export interface MonitorEvent {
  level: string
  message: string
  stacktrace?: string
  context?: Record<string, unknown>
}

export interface ErrorEventInput {
  message: string
  stacktrace?: string
  context_json?: Record<string, unknown>
}

export interface Breadcrumb {
  type: string
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
