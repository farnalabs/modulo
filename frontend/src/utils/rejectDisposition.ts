/**
 * FAR-1487: what a rejection does at a HITL gate — `route` (a reject
 * destination exists), `terminate` (the default: the run ENDS `rejected`) or
 * `proceed` (explicit `on_reject: proceed` / a correction target: the run
 * continues down the approve path). Absent on briefings captured before
 * FAR-1487.
 *
 * Shared by HitlBriefing and HitlReviewCard so the closed-vocabulary guard
 * cannot drift between the two surfaces.
 */
export type RejectDisposition = 'route' | 'terminate' | 'proceed'

export function asRejectDisposition(value: unknown): RejectDisposition | null {
  return value === 'route' || value === 'terminate' || value === 'proceed' ? value : null
}
