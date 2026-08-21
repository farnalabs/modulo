export interface Breadcrumb {
  type: 'click' | 'navigation' | 'api' | 'route_change'
  timestamp: string
  data: Record<string, unknown>
}

export type { ErrorEventInput } from '../../monitor/types'

export interface ErrorIngestResponse {
  results: ErrorGroupResult[]
}

export interface ErrorGroupResult {
  group_id: string
  is_new: boolean
}

export interface SessionKeyResponse {
  key: string
}

import type { MonitorBackend } from '../../monitor/types'

export interface ErrorTrackerConfig {
  appName?: string
  environment?: string
  version?: string
  flushIntervalMs?: number
  batchSize?: number
  monitorBackends?: MonitorBackend[]
}
