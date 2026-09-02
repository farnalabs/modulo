import { api } from './client'
import { formatApiError, throwOnError } from './formatError'

export interface RunListItem extends Record<string, unknown> {
  run_id: string
  pipeline_id: string
  pipeline_name: string | null
  status: string
  trigger_type: string
  run_number: number | null
  created_at: string | null
  started_at: string | null
  completed_at: string | null
  error_code: string | null
  error_detail?: string | null
  total_cost_usd: number | null
  account_id: string | null
  child_runs_cost_usd?: string | null
  child_runs_count?: number
  aggregate_cost_usd?: string | null
  input_payload?: Record<string, unknown> | null
  trigger_actor?: string | null
  heartbeat_at?: string | null
  capacity?: { active_runs: number; concurrency_limit: number | null; waiting: boolean } | null
}

export interface RunListResponse {
  items: RunListItem[]
  total: number
  page: number
  page_size: number
  next_cursor: string | null
  has_more: boolean
}

export interface FetchRunsParams {
  status?: string
  trigger_type?: string
  search?: string
  pipeline_id?: string
  page?: number
  page_size?: number
  cursor?: string
}

type GetRuns = (
  path: '/api/v1/runs',
  options: { params: { query: Record<string, unknown> } },
) => Promise<{ data?: RunListResponse; error?: unknown }>

export async function fetchRuns(params: FetchRunsParams = {}): Promise<RunListResponse> {
  const q: Record<string, unknown> = {}
  if (params.status) q.status = params.status
  if (params.trigger_type) q.trigger_type = params.trigger_type
  if (params.search) q.search = params.search
  if (params.pipeline_id) q.pipeline_id = params.pipeline_id
  if (params.page !== undefined) q.page = params.page
  if (params.page_size !== undefined) q.page_size = params.page_size
  if (params.cursor) q.cursor = params.cursor
  // Isolate openapi-fetch's recursive path inference for this broad list response.
  const getRuns = api.GET as unknown as GetRuns
  return throwOnError(await getRuns('/api/v1/runs', {
    params: { query: q },
  }))
}

/**
 * Requests cancellation of a run. Never throws — returns a formatted error
 * string on failure so callers can render it directly in their inline error UI.
 * @param errorPrefix the already-translated user-facing prefix for error
 *   messages, e.g. `t('views.RunsListView.cancel_failed')`.
 */
export async function requestRunCancellation(runId: string, errorPrefix: string): Promise<{ error?: string }> {
  try {
    const { error } = await api.POST('/api/v1/runs/{run_id}/cancel', {
      params: { path: { run_id: runId } },
    })
    if (error) return { error: `${errorPrefix} ${formatApiError(error)}` }
    return {}
  } catch (e: unknown) {
    return { error: `${errorPrefix} ${formatApiError(e)}` }
  }
}
