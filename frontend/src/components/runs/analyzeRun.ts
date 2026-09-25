import { shortId } from '@/utils/format'

/**
 * Run facts the Analyze action hands to the Assistant for a root-cause
 * analysis (FAR-1235). Built by RunDetailView from the run detail + IO
 * responses; `failingNode` is optional because it is only known when the
 * per-node telemetry (or a live WebSocket session) recorded it.
 */
export interface AnalyzeRunInfo {
  runId: string
  runNumber: number | null
  pipelineId: string
  pipelineName: string | null
  status: string
  errorCode: string | null
  errorDetail: string | null
  failingNode: string | null
}

/**
 * Terminal run outcomes that represent a FAILURE worth analysing: every
 * terminal status except `complete` (the run succeeded) and `cancelled`
 * (a user stopped it deliberately — there is no failure to root-cause).
 *
 * Kept in sync with TERMINAL_STATUSES in constants/runStatuses.ts; a future
 * terminal failure status must be added here to get the Analyze action.
 */
export const ANALYZABLE_FAILURE_STATUSES = [
  'failed',
  'eval_failed',
  'stalled',
  'budget_exceeded',
  'cost_ceiling_exceeded',
  'router_no_match',
  'compensation_failed',
] as const

export function isAnalyzableFailure(status: string): boolean {
  return (ANALYZABLE_FAILURE_STATUSES as readonly string[]).includes(status)
}

/** `#123` when the run carries a number, otherwise the short run id. */
export function runLabel(run: AnalyzeRunInfo): string {
  return run.runNumber != null ? `#${run.runNumber}` : shortId(run.runId)
}

/** Pipeline name when present, otherwise the short pipeline id. */
export function pipelineLabel(run: AnalyzeRunInfo): string {
  return run.pipelineName || shortId(run.pipelineId)
}

/** Error detail is persisted as unbounded text; cap what we quote to the model. */
export const MAX_ERROR_DETAIL_CHARS = 4000

/** Minimal shape of vue-i18n's `t` so the seed message stays unit-testable. */
export type TranslateFn = (key: string, named?: Record<string, string | number>) => string

/**
 * Build the opening user message of the analysis conversation: which run
 * failed (identity + pipeline + status), what the error was (code / detail /
 * failing node when available), and the root-cause-analysis request itself.
 *
 * All human-readable text comes from locale keys; only DATA (ids, names,
 * error text) is interpolated, never logic inside a translation value.
 */
export function buildAnalyzeSeedMessage(t: TranslateFn, run: AnalyzeRunInfo): string {
  const lines: string[] = [
    t('components.AnalyzeRunButton.seed_header', {
      run: runLabel(run),
      pipeline: pipelineLabel(run),
      status: run.status,
    }),
  ]
  if (run.errorCode) {
    lines.push(t('components.AnalyzeRunButton.seed_error_code', { code: run.errorCode }))
  }
  if (run.errorDetail) {
    lines.push(
      t('components.AnalyzeRunButton.seed_error_detail', {
        detail: run.errorDetail.slice(0, MAX_ERROR_DETAIL_CHARS),
      }),
    )
  }
  if (run.failingNode) {
    lines.push(t('components.AnalyzeRunButton.seed_failing_node', { node: run.failingNode }))
  }
  lines.push(t('components.AnalyzeRunButton.seed_request'))
  return lines.join('\n')
}
