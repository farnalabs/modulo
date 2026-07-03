export interface UserInfo {
  id: string
  email?: string
  username?: string
}

export interface MonitorConfig {
  enabled?: boolean
  environment?: string
}

export interface MonitorEvent {
  level: string
  message: string
  stacktrace?: string
  context_json?: Record<string, unknown>
  source?: string
  environment?: string
  version?: string
}

export interface MonitorBackend {
  captureError(event: MonitorEvent, error: Error, context?: Record<string, unknown>): void
  captureMessage(message: string, level: string): void
  setUser(user: UserInfo | null): void
  setTags(tags: Record<string, string>): void
  dispose(): void
}
