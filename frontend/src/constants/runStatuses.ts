/**
 * Centralized run-status classification constants shared across run views.
 * These match the DB CHECK constraint: status IN ('pending', 'running', 'awaiting_human', 'claimed', 'unknown', 'complete', 'failed', 'cancelled', 'eval_failed', 'stalled', 'budget_exceeded', 'router_no_match', 'cost_ceiling_exceeded', 'compensation_failed')
 * Used by RunsListView (non-terminal → show the Cancel action) and RunDetailView (terminal → hide Cancel / stop polling).
 * 'stalled' is terminal: a sandbox agent that went silent past the idle watchdog had its sandbox killed.
 * 'budget_exceeded' is terminal: the cost controller finalized the run when the per-agent token budget was breached.
 * 'cost_ceiling_exceeded' is terminal: the cost controller finalized the run when the org-wide spend ceiling was breached.
 * 'router_no_match' is terminal (FAR-402 P1): a Router node had no matching rule and no default.
 * 'compensation_failed' is terminal: a watched node AND its compensation path both failed (FAR-402 P5).
 * 'unknown' is NON-terminal: the run's outcome could not be determined but it is not finalised (recovery status, FAR-410).
 */
export const TERMINAL_STATUSES = ['complete', 'failed', 'cancelled', 'eval_failed', 'stalled', 'budget_exceeded', 'router_no_match', 'cost_ceiling_exceeded', 'compensation_failed'] as const

export const NON_TERMINAL_STATUSES = ['pending', 'running', 'awaiting_human', 'claimed', 'unknown'] as const

export function isTerminalStatus(status: string): boolean {
  return (TERMINAL_STATUSES as readonly string[]).includes(status)
}

export function isNonTerminalStatus(status: string): boolean {
  return (NON_TERMINAL_STATUSES as readonly string[]).includes(status)
}
