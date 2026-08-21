import { useApi } from '../../composables/useApi'
import { formatApiError } from './formatError'

/**
 * Batch-scoped variant comparison API surface (FAR-332, "Generalised Variant
 * Comparison Workflow").
 *
 * A variant *batch* is a frozen firing of a variant group: every variant runs
 * against the same pipeline snapshot with its own `run_context_overrides`.
 * The batch carries a server-computed completion signal and survives
 * soft-delete of its source group, so the compare URL and run links keep
 * working after the group is hidden. All reads are scoped by `batch_id` —
 * never by a live group.
 *
 * The backend routes live behind `useApi` (untyped string paths) rather than
 * the generated schema until the parallel backend worktree lands them in the
 * OpenAPI spec. See the parallel backend worktree for the route handlers.
 */

export type VariantRunStatus =
  | 'pending'
  | 'running'
  | 'awaiting_human'
  | 'claimed'
  | 'complete'
  | 'failed'
  | 'cancelled'
  | 'eval_failed'
  | 'stalled'
  | 'budget_exceeded'

export type VariantBatchStatus = 'pending' | 'running' | 'partial' | 'complete' | 'failed' | 'cancelled'

export interface VariantBatchRunEval {
  eval_id: string
  node_id: string
  passed: boolean
  score: number | null
  detail?: string | null
}

export interface VariantBatchRun {
  run_id: string
  variant_name: string
  /** The frozen snapshot / input label this variant ran with. */
  snapshot_label: string | null
  /** Human description of the run_context_overrides diff for this variant. */
  input_label: string | null
  run_status: VariantRunStatus
  pass_rate: number | null
  total_cost_usd: number | null
  total_tokens: number | null
  eval_results: VariantBatchRunEval[]
  node_outputs: Record<string, unknown> | null
}

export interface VariantBatchDetail {
  batch_id: string
  name: string
  pipeline_id: string
  pipeline_name: string | null
  status: VariantBatchStatus
  created_at: string
  updated_at: string
  runs: VariantBatchRun[]
}

export interface VariantBatchSummary {
  batch_id: string
  name: string
  pipeline_name: string | null
  status: VariantBatchStatus
  run_count: number
  created_at: string
}

export interface VariantBatchListResponse {
  items: VariantBatchSummary[]
  total: number
}

const api = useApi()

/** Fetch a single batch's detail + runs by batch_id. Never throws — returns a formatted error string. */
export async function fetchVariantBatch(batchId: string): Promise<{ data?: VariantBatchDetail; error?: string }> {
  try {
    const data = await api.get<VariantBatchDetail>(`/api/v1/variant-batches/${batchId}`)
    return { data }
  } catch (e: unknown) {
    return { error: formatApiError(e) }
  }
}

/** List the user's variant batches ("My comparisons"), soft-delete aware. Never throws. */
export async function fetchVariantBatches(): Promise<{ data?: VariantBatchListResponse; error?: string }> {
  try {
    const data = await api.get<VariantBatchListResponse>('/api/v1/variant-batches')
    return { data }
  } catch (e: unknown) {
    return { error: formatApiError(e) }
  }
}

/** Soft-delete a batch: hides it from "My comparisons" but keeps the compare URL + run links working. */
export async function softDeleteVariantBatch(batchId: string): Promise<{ error?: string }> {
  try {
    await api.delete<void>(`/api/v1/variant-batches/${batchId}`)
    return {}
  } catch (e: unknown) {
    return { error: formatApiError(e) }
  }
}

/** Re-fire a batch from its frozen definition. Returns the new batch detail. */
export async function reFireVariantBatch(batchId: string): Promise<{ data?: VariantBatchDetail; error?: string }> {
  try {
    const data = await api.post<VariantBatchDetail>(`/api/v1/variant-batches/${batchId}/re-fire`)
    return { data }
  } catch (e: unknown) {
    return { error: formatApiError(e) }
  }
}
