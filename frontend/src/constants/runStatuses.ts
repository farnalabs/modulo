/**
 * Centralized run-status classification constants shared across run views.
 * These match the DB CHECK constraint: status IN ('pending', 'running', 'awaiting_human', 'claimed', 'unknown', 'hitl_parked', 'complete', 'failed', 'cancelled', 'eval_failed', 'stalled', 'budget_exceeded', 'router_no_match', 'cost_ceiling_exceeded', 'compensation_failed')
 * Used by RunsListView (non-terminal → show the Cancel/Stop action) and RunDetailView (terminal → hide Cancel / stop polling).
 * 'stalled' is terminal: a sandbox agent that went silent past the idle watchdog had its sandbox killed.
 * 'budget_exceeded' is terminal: the cost controller finalized the run when the per-agent token budget was breached.
 * 'cost_ceiling_exceeded' is terminal: the cost controller finalized the run when the org-wide spend ceiling was breached.
 * 'router_no_match' is terminal (FAR-402 P1): a Router node had no matching rule and no default.
 * 'compensation_failed' is terminal: a watched node AND its compensation path both failed (FAR-402 P5).
 * 'unknown' is NON-terminal: the run's outcome could not be determined but it is not finalised (recovery status, FAR-410).
 * 'hitl_parked' is NON-terminal (FAR-604 D2): the run's HITL gate expired unanswered and the park sweep moved it
 * out of 'awaiting_human'; the gate stays open and claimable and a decision (or the dispatcher reconcile)
 * re-enters the run into normal admission.
 */
export const TERMINAL_STATUSES = ['complete', 'failed', 'cancelled', 'eval_failed', 'stalled', 'budget_exceeded', 'router_no_match', 'cost_ceiling_exceeded', 'compensation_failed'] as const

export const NON_TERMINAL_STATUSES = ['pending', 'running', 'awaiting_human', 'claimed', 'unknown', 'hitl_parked'] as const

export function isTerminalStatus(status: string): boolean {
  return (TERMINAL_STATUSES as readonly string[]).includes(status)
}

/**
 * Derived as the complement of TERMINAL_STATUSES (qa F4): an unknown FUTURE
 * status can never be misclassified as terminal (which would hide the
 * Cancel/Stop affordance and stop polling for a live run) — the two
 * classification lists cannot drift apart because only one is authoritative.
 */
export function isNonTerminalStatus(status: string): boolean {
  return !isTerminalStatus(status)
}
