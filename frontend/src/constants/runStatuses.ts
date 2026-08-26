/**
 * Centralized run-status classification constants shared across run views.
 * These match the DB CHECK constraint: status IN ('pending', 'running', 'awaiting_human', 'claimed', 'complete', 'failed', 'cancelled', 'eval_failed', 'stalled', 'budget_exceeded', 'router_no_match')
 * Used by RunsListView (non-terminal → show the Cancel action) and RunDetailView (terminal → hide Cancel / stop polling).
 * 'stalled' is terminal: a sandbox agent that went silent past the idle watchdog had its sandbox killed.
 * 'budget_exceeded' is terminal: the cost controller finalized the run when the per-agent token budget was breached.
 * 'router_no_match' is terminal (FAR-402 P1): a Router node had no matching rule and no default.
 */
export const TERMINAL_STATUSES = ['complete', 'failed', 'cancelled', 'eval_failed', 'stalled', 'budget_exceeded', 'router_no_match'] as const

export const NON_TERMINAL_STATUSES = ['pending', 'running', 'awaiting_human', 'claimed'] as const

export function isTerminalStatus(status: string): boolean {
  return (TERMINAL_STATUSES as readonly string[]).includes(status)
}

export function isNonTerminalStatus(status: string): boolean {
  return (NON_TERMINAL_STATUSES as readonly string[]).includes(status)
}
