import { api } from './client'
import { throwOnError } from './formatError'

export interface RunListItem {
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
  total_cost_usd: number | null
  account_id: string | null
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
}

export async function fetchRuns(params: FetchRunsParams = {}): Promise<RunListResponse> {
  const q: Record<string, unknown> = {}
  if (params.status) q.status = params.status
  if (params.trigger_type) q.trigger_type = params.trigger_type
  if (params.search) q.search = params.search
  if (params.pipeline_id) q.pipeline_id = params.pipeline_id
  if (params.page) q.page = params.page
  if (params.page_size) q.page_size = params.page_size
  return throwOnError(await api.GET('/api/v1/runs', {
    params: { query: q },
  })) as RunListResponse
}
