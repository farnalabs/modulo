import type { components } from '../../lib/api/schema'

/**
 * Presentation helpers shared by the determination proposal and review panels.
 *
 * The confidence vocabulary is the contract's own (``high`` / ``medium`` /
 * ``low`` / ``unknown``); these helpers only map a value onto house styling and
 * the "blocks approval" rule that the backend applies in
 * ``determination/review.py`` (``_UNSETTLED_CONFIDENCE``). Keeping the rule in
 * one place stops the panel's disabled-state hint from disagreeing with the
 * server's 422.
 */

export type ProposalConfidence = components['schemas']['ProposalFact']['confidence']

/** Badge classes for a confidence value. */
export function confidenceBadgeClass(confidence: ProposalConfidence | string): string {
  switch (confidence) {
    case 'high':
      return 'bg-success/10 text-success-text'
    case 'medium':
      return 'bg-warning/10 text-warning-text'
    case 'low':
      return 'bg-destructive/10 text-destructive-text'
    default:
      return 'bg-muted text-muted-foreground'
  }
}

/**
 * True for the confidence values that block approval until the reviewer
 * acknowledges the assumption by id — mirrors the backend's
 * ``_UNSETTLED_CONFIDENCE`` set.
 */
export function isUnsettledConfidence(confidence: string): boolean {
  return confidence === 'low' || confidence === 'unknown'
}
