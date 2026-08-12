/**
 * Centralized filter value constants for run statuses.
 * These match the DB CHECK constraint: status IN ('pending', 'running', 'awaiting_human', 'claimed', 'complete', 'failed', 'cancelled', 'eval_failed', 'stalled')
 * These are used across DashboardView, RunsListView, etc.
 */
export const RUN_STATUS = {
  PENDING: 'pending',
  RUNNING: 'running',
  AWAITING_HUMAN: 'awaiting_human' as const,
  CLAIMED: 'claimed',
  COMPLETE: 'complete',
  FAILED: 'failed' as const,
  CANCELLED: 'cancelled',
  EVAL_FAILED: 'eval_failed',
  STALLED: 'stalled' as const,
} as const;

export type RunStatus = typeof RUN_STATUS[keyof typeof RUN_STATUS];

export const TRIGGER_TYPE = {
  MANUAL: 'manual',
  WEBHOOK: 'webhook',
  CRON: 'cron',
  CORRECTION: 'correction',
} as const;

export type TriggerType = typeof TRIGGER_TYPE[keyof typeof TRIGGER_TYPE];
