import type { components } from '../../lib/api/schema'

/**
 * Presentation helpers for the determination migration comparison (FAR-2473).
 *
 * The comparison result is the backend's deterministic, read-only difference
 * (``modulo.determination.comparison``); these helpers only map it onto house
 * styling and translated labels. They are free of Vue state so they can be
 * unit-tested directly.
 *
 * Two rules drive them:
 *
 * 1. Only the categories the response contains are ever rendered. A behaviour
 *    change kind outside the contract's vocabulary falls back to a labelled
 *    rendering of the value itself - it is never mapped onto a category the
 *    backend did not report.
 * 2. A property value of ``null`` means the node or edge is absent from that
 *    side; it is never rendered as the string "null" or as a boolean.
 */

export type ProposalComparison = components['schemas']['ProposalComparison']
export type ProvenanceBundle = components['schemas']['ProvenanceBundle']
export type ProposalSideSummary = components['schemas']['ProposalSideSummary']
export type BehaviourChange = components['schemas']['BehaviourChange']

/** Which side of the comparison a rendered item came from. */
export type ComparisonSide = ProposalSideSummary['side']

/** A property value as it should be rendered: absent, boolean, or free text. */
export type PropertyValueView =
  | { kind: 'absent' }
  | { kind: 'boolean'; value: boolean }
  | { kind: 'text'; value: string }

/** True when the comparison reported no structural or behaviour difference. */
export function comparisonIsEmpty(comparison: ProposalComparison): boolean {
  return [
    comparison.nodes_added,
    comparison.nodes_removed,
    comparison.nodes_changed,
    comparison.edges_added,
    comparison.edges_removed,
    comparison.edges_changed,
    comparison.behaviour_changes,
  ].every((items) => (items ?? []).length === 0)
}

/**
 * Badge classes that keep current (base) content quiet and proposed (target)
 * content prominent, so the two sides are never mistaken for each other.
 */
export function sideBadgeClass(side: ComparisonSide): string {
  return side === 'base' ? 'bg-muted text-muted-foreground' : 'bg-warning/10 text-warning-text'
}

/** The contract's own behaviour-change vocabulary mapped onto locale keys. */
const BEHAVIOUR_LABEL_KEYS: Readonly<Record<string, string>> = {
  approval_gate_added: 'comparison_behaviour_approval_gate_added',
  approval_gate_removed: 'comparison_behaviour_approval_gate_removed',
  manual_step_added: 'comparison_behaviour_manual_step_added',
  manual_step_removed: 'comparison_behaviour_manual_step_removed',
  step_automated: 'comparison_behaviour_step_automated',
  step_deautomated: 'comparison_behaviour_step_deautomated',
}

/**
 * The locale key suffix for a behaviour-change kind.
 *
 * An out-of-vocabulary kind must not be presented as one of the known
 * categories, so it falls back to a labelled rendering of the value itself.
 */
export function behaviourLabelKey(kind: string): string {
  return BEHAVIOUR_LABEL_KEYS[kind] ?? 'comparison_behaviour_other'
}

/** The renderable shape of a property value. */
export function propertyValueView(value: string | boolean | null): PropertyValueView {
  if (value === null) return { kind: 'absent' }
  if (typeof value === 'boolean') return { kind: 'boolean', value }
  return { kind: 'text', value }
}

/** A stable key for a behaviour-change row (never its array position). */
export function behaviourChangeKey(change: BehaviourChange): string {
  return JSON.stringify([change.kind, change.subject_kind, change.subject_id, change.property])
}
