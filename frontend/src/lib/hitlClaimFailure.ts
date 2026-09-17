import type { ComposerTranslation } from 'vue-i18n'
import { formatApiError } from './api/formatError'

/**
 * FAR-645 / FAR-861: discriminate claim conflicts by the backend's machine-
 * readable problem type (urn:problem:modulo:<type>), not by substring-matching
 * English prose -- a backend rewording can no longer degrade the UX. Shared by
 * the single-gate card (HitlGateCard) and the bulk review path
 * (SettingsHitlReviewView) so the problem-type discrimination cannot drift
 * between the two. The detail text still flows into the generic fallback so an
 * unrecognised type keeps rendering the backend's explanation.
 */
export function claimFailureMessage(err: unknown, t: ComposerTranslation): string {
  const detail = formatApiError(err)
  const problemType = typeof err === 'object' && err !== null
    ? (err as Record<string, unknown>).type
    : undefined
  if (problemType === 'urn:problem:modulo:hitl_gate_already_claimed') {
    return t('hitl.gate.claim_failed_already_claimed')
  }
  if (problemType === 'urn:problem:modulo:hitl_gate_already_decided') {
    return t('hitl.gate.claim_failed_already_decided')
  }
  if (problemType === 'urn:problem:modulo:hitl_run_not_awaiting') {
    return t('hitl.gate.claim_failed_run_not_awaiting', { reason: detail })
  }
  return `${t('hitl.gate.claim_failed')} ${detail}`
}
