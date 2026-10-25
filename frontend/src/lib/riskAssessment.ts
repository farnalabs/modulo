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
