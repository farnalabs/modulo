/**
 * Shared vocabulary and default state for the risk/assessment projection
 * (FAR-941). Single source for the gate-action list, the filter defaults and
 * the wire query builder — the store, the view and the facets all consume
 * these instead of re-listing literals.
 */

/** Gate actions the projection can report, most severe first. */
export const RISK_ACTIONS = ['block', 'escalate', 'warn', 'continue'] as const

export type RiskAction = (typeof RISK_ACTIONS)[number]

export interface RiskAssessmentFilters {
  keyPrefix: string
  pipelineId: string
  days: number
  action: string
  limit: number
}

export interface EvidenceRow {
  id: string
  organisation_id: string
  key: string
  subject_type: string
  subject_id: string
  value: unknown
  observed_at: string
  producer_type: string
  producer_id: string | null
  created_at: string
  updated_at: string
  action: string | null
}

export interface RiskAssessmentSummary {
  total_rows: number
  action_counts: Record<string, number>
  facet_degraded: boolean
}

export interface RiskAssessmentResponse {
  summary: RiskAssessmentSummary
  rows: EvidenceRow[]
}

/** The one place filter defaults are defined. */
export function defaultRiskAssessmentFilters(): RiskAssessmentFilters {
  return {
    keyPrefix: '',
    pipelineId: '',
    days: 7,
    action: '',
    limit: 100,
  }
}

/** Wire query shape for the projection (matches the generated operation). */
export interface RiskAssessmentQuery {
  key_prefix?: string
  pipeline_id?: string
  days?: number
  action?: string
  limit?: number
}

/** Translate form state into the wire query, omitting empty filters. */
export function buildRiskAssessmentQuery(filters: RiskAssessmentFilters): RiskAssessmentQuery {
  const params: RiskAssessmentQuery = {}
  if (filters.keyPrefix) params.key_prefix = filters.keyPrefix
  if (filters.pipelineId) params.pipeline_id = filters.pipelineId
  if (filters.days) params.days = filters.days
  if (filters.action) params.action = filters.action
  if (filters.limit) params.limit = filters.limit
  return params
}

/**
 * The time windows the facet select offers — the ONE source for both the
 * rendered options and the values a deep link may carry, so the two can never
 * drift apart. Labels are `views.RiskAssessmentView.<labelKey>` i18n keys.
 */
export const RISK_WINDOW_CHOICES: ReadonlyArray<{ value: number; labelKey: string }> = [
  { value: 1, labelKey: 'last_1_day' },
  { value: 7, labelKey: 'last_7_days' },
  { value: 30, labelKey: 'last_30_days' },
  { value: 90, labelKey: 'last_90_days' },
]

const RISK_WINDOW_VALUES: ReadonlySet<number> = new Set(
  RISK_WINDOW_CHOICES.map((choice) => choice.value),
)

/** The wire form the API's ``pipeline_id: uuid.UUID`` query parameter parses —
 * anything else is answered 422, so a deep link carrying it is a 422-in-waiting. */
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** Whole-string integers only: ``parseInt`` would silently accept ``"30abc"``. */
const INTEGER_PATTERN = /^-?\d+$/

/**
 * Hydrate deep-linked filters from a route query, keeping ONLY values the
 * facet UI itself can express and the wire contract accepts.
 *
 * A deep link is written by this same surface, so the facet vocabulary (the
 * offered time windows, the gate actions, the listed pipeline ids) defines the
 * link format. A value outside it is a malformed link — a stale bookmark or a
 * hand-tampered URL — and degrades to the default rather than reaching the API,
 * because the backend answers such values 422 and the error alert's Retry
 * re-applies the same filters: an out-of-contract hydration turns a malformed
 * link into an error the retry can never clear. ``key_prefix`` is free text on
 * both sides (the API treats it as a LIKE prefix) and passes through as before.
 */
export function riskFiltersFromRouteQuery(query: Record<string, unknown>): RiskAssessmentFilters {
  const filters = defaultRiskAssessmentFilters()

  const keyPrefix = query.key_prefix
  if (typeof keyPrefix === 'string') filters.keyPrefix = keyPrefix

  const pipelineId = query.pipeline_id
  if (typeof pipelineId === 'string' && UUID_PATTERN.test(pipelineId)) {
    filters.pipelineId = pipelineId
  }

  const days = query.days
  if (typeof days === 'string' && INTEGER_PATTERN.test(days)) {
    const parsed = Number.parseInt(days, 10)
    if (RISK_WINDOW_VALUES.has(parsed)) filters.days = parsed
  }

  const action = query.action
  if (typeof action === 'string' && (RISK_ACTIONS as readonly string[]).includes(action)) {
    filters.action = action
  }

  return filters
}

/** Project filters into the URL query used for deep-linking the view. */
export function riskAssessmentRouteQuery(
  filters: RiskAssessmentFilters,
): Record<string, string> {
  const query: Record<string, string> = {}
  if (filters.keyPrefix) query.key_prefix = filters.keyPrefix
  if (filters.pipelineId) query.pipeline_id = filters.pipelineId
  query.days = String(filters.days)
  if (filters.action) query.action = filters.action
  return query
}
