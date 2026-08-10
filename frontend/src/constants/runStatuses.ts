/**
 * Centralized run-status classification constants shared across run views.
 * These match the DB CHECK constraint: status IN ('pending', 'running', 'awaiting_human', 'claimed', 'waiting_for_lock', 'complete', 'failed', 'cancelled', 'eval_failed')
 * Used by RunsListView (non-terminal → show the Cancel action) and RunDetailView (terminal → hide Cancel / stop polling).
 */
export const TERMINAL_STATUSES = ['complete', 'failed', 'cancelled', 'eval_failed'] as const

export const NON_TERMINAL_STATUSES = ['pending', 'running', 'awaiting_human', 'claimed', 'waiting_for_lock'] as const

export function isTerminalStatus(status: string): boolean {
  return (TERMINAL_STATUSES as readonly string[]).includes(status)
}

export function isNonTerminalStatus(status: string): boolean {
  return (NON_TERMINAL_STATUSES as readonly string[]).includes(status)
}
