import type { components } from '../../lib/api/schema'

/**
 * Presentation helpers for the determination rerun comparison (FAR-2474).
 *
 * The rerun result is the backend's pure, read-only comparison
 * (``modulo.determination.rerun``); these helpers only map it onto house
 * styling and the classification vocabulary. They are free of Vue state so
 * they can be unit-tested directly.
 *
 * Two rules drive them:
 *
 * 1. ``not_reproduced`` and ``removed`` are different facts and must never be
 *    presented alike. ``not_reproduced`` means the sampled inputs were
 *    unchanged and the fresh attempt did not reproduce the inference;
 *    ``removed`` means the inputs themselves changed. Neither asserts the
 *    inference is false or wrong, so neither is styled as a failure.
 * 2. Only the categories and source changes the response actually contains are
 *    rendered. A missing list means "not reported", never "empty change".
 */

export type RerunResult = components['schemas']['RerunResult']
export type RerunAttemptSummary = components['schemas']['RerunAttemptSummary']
export type ElementChange = components['schemas']['ElementChange']
export type ElementChangeStatus = ElementChange['status']
export type SourceInventoryChanges = components['schemas']['SourceInventoryChanges']
export type SourceInventoryItem = components['schemas']['SourceInventoryItem']
export type SourceSampleCountChange = components['schemas']['SourceSampleCountChange']
export type CarriedForwardCorrections = components['schemas']['CarriedForwardCorrections']

/** The contract's own classification vocabulary, in a stable display order. */
export const ELEMENT_CHANGE_STATUSES: readonly ElementChangeStatus[] = [
  'retained',
  'new',
  'not_reproduced',
  'removed',
  'confidence_changed',
  'evidence_changed',
]

/** The changes the backend classified with ``status``. */
export function changesByStatus(changes: ElementChange[], status: ElementChangeStatus): ElementChange[] {
  return changes.filter((change) => change.status === status)
}

/** One count per classification, including the classifications with none. */
export function elementChangeCounts(changes: ElementChange[]): Record<ElementChangeStatus, number> {
  const counts: Record<ElementChangeStatus, number> = {
    retained: 0,
    new: 0,
    not_reproduced: 0,
    removed: 0,
    confidence_changed: 0,
    evidence_changed: 0,
  }
  for (const change of changes) counts[change.status] += 1
  return counts
}

/** A stable row key (never the array position). */
export function elementChangeKey(change: ElementChange): string {
  return JSON.stringify([
    change.status,
    change.prior_element_id ?? null,
    change.current_element_id ?? null,
    change.statement,
  ])
}

/**
 * Badge styling per classification.
 *
 * ``not_reproduced`` stays quiet/muted while ``removed`` is the only
 * destructive styling in the set, so the two can never be read as the same
 * outcome. No classification is styled as an error.
 */
export function elementChangeBadgeClass(status: ElementChangeStatus): string {
  switch (status) {
    case 'retained':
      return 'bg-muted text-muted-foreground'
    case 'new':
      return 'bg-success/10 text-success-text'
    case 'not_reproduced':
      return 'bg-muted text-muted-foreground'
    case 'removed':
      return 'bg-destructive/10 text-destructive-text'
    case 'confidence_changed':
      return 'bg-warning/10 text-warning-text'
    default:
      return 'bg-primary/10 text-primary'
  }
}

/** Group-container styling; only ``removed`` is destructive. */
export function elementChangeGroupClass(status: ElementChangeStatus): string {
  switch (status) {
    case 'removed':
      return 'border-destructive/40'
    case 'not_reproduced':
      return 'border-dashed'
    default:
      return ''
  }
}

/** True when any sampled-input difference was reported. */
export function inputChangesPresent(changes: SourceInventoryChanges | null | undefined): boolean {
  if (!changes) return false
  return Boolean(
    (changes.new_sources ?? []).length ||
      (changes.missing_sources ?? []).length ||
      (changes.newly_available ?? []).length ||
      (changes.newly_unavailable ?? []).length ||
      (changes.sample_count_changes ?? []).length,
  )
}

/**
 * True when the result must be presented as incomplete.
 *
 * The backend marks a rerun incomplete for a ``completed_with_gaps`` fresh
 * attempt or a source the fresh scan no longer produced. A fresh attempt that
 * reports gaps, or a source that flipped to unavailable, is the same class of
 * gap - the fresh attempt did not read the full input set - so the surface
 * treats those as incomplete too, even when the backend's ``incomplete`` flag
 * does not repeat them.
 */
export function rerunReportsIncomplete(result: RerunResult): boolean {
  if (result.incomplete) return true
  if (result.current_attempt.status === 'completed_with_gaps') return true
  return (
    (result.source_changes.newly_unavailable ?? []).length > 0 ||
    (result.source_changes.missing_sources ?? []).length > 0
  )
}

/** Corrections the fresh attempt accepted, by kind. */
export function matchedCorrectionCount(corrections: CarriedForwardCorrections): number {
  return (corrections.corrected_derived_element_ids ?? []).length + (corrections.removed_derived_element_ids ?? []).length
}

/** Corrections that targeted an element the fresh attempt did not produce. */
export function unmatchedCorrectionCount(corrections: CarriedForwardCorrections): number {
  return (
    (corrections.unmatched_corrected_derived_element_ids ?? []).length +
    (corrections.unmatched_removed_derived_element_ids ?? []).length
  )
}

/** True when the response reports no correction at all (matched or not). */
export function correctionsAreEmpty(corrections: CarriedForwardCorrections): boolean {
  return matchedCorrectionCount(corrections) === 0 && unmatchedCorrectionCount(corrections) === 0
}

/** A stable key for a source-inventory item (never the array position). */
export function sourceItemKey(item: SourceInventoryItem): string {
  return JSON.stringify([item.source_id, item.sample_count, item.status])
}

/** A stable key for a sample-count change row. */
export function sampleCountChangeKey(change: SourceSampleCountChange): string {
  return JSON.stringify([change.source_id, change.prior_sample_count, change.current_sample_count])
}
