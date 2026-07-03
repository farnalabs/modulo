export interface Breadcrumb {
  type: 'click' | 'navigation' | 'api' | 'route_change'
  timestamp: string
  data: Record<string, unknown>
}

export interface ErrorEventInput {
  level: 'error' | 'warning' | 'critical'
  message: string
  stacktrace?: string
  context_json?: Record<string, unknown>
  source?: string
  environment?: string
  version?: string
  breadcrumbs?: Breadcrumb[]
}

export interface ErrorIngestResponse {
  results: ErrorGroupResult[]
}

export interface ErrorGroupResult {
  group_id: string
  is_new: boolean
}

export interface SessionKeyResponse {
  session_key: string
}

export interface ErrorTrackerConfig {
  appName?: string
  environment?: string
  version?: string
  flushIntervalMs?: number
  batchSize?: number
  monitorBackends?: string[]
}
