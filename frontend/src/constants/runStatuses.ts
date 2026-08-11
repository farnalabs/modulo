/**
 * Centralized run-status classification constants shared across run views.
 * These match the DB CHECK constraint: status IN ('pending', 'running', 'awaiting_human', 'claimed', 'complete', 'failed', 'cancelled', 'eval_failed', 'stalled')
 * Used by RunsListView (non-terminal → show the Cancel action) and RunDetailView (terminal → hide Cancel / stop polling).
 * 'stalled' is terminal: a sandbox agent that went silent past the idle watchdog had its sandbox killed.
 */
export const TERMINAL_STATUSES = ['complete', 'failed', 'cancelled', 'eval_failed', 'stalled'] as const

export const NON_TERMINAL_STATUSES = ['pending', 'running', 'awaiting_human', 'claimed'] as const

export function isTerminalStatus(status: string): boolean {
  return (TERMINAL_STATUSES as readonly string[]).includes(status)
}

export function isNonTerminalStatus(status: string): boolean {
  return (NON_TERMINAL_STATUSES as readonly string[]).includes(status)
}
