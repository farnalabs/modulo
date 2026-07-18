/**
 * Centralized filter value constants for run statuses.
 * These match the DB CHECK constraint: status IN ('pending', 'running', 'awaiting_human', 'claimed', 'waiting_for_lock', 'complete', 'failed', 'cancelled', 'eval_failed')
 * These are used across DashboardView, RunsListView, StageBoardView, etc.
 */
export const RUN_STATUS = {
  PENDING: 'pending',
  RUNNING: 'running',
  AWAITING_HUMAN: 'awaiting_human' as const,
  CLAIMED: 'claimed',
  WAITING_FOR_LOCK: 'waiting_for_lock',
  COMPLETE: 'complete',
  FAILED: 'failed' as const,
  CANCELLED: 'cancelled',
  EVAL_FAILED: 'eval_failed',
} as const;

export type RunStatus = typeof RUN_STATUS[keyof typeof RUN_STATUS];

export const TRIGGER_TYPE = {
  MANUAL: 'manual',
  WEBHOOK: 'webhook',
  CRON: 'cron',
  CORRECTION: 'correction',
} as const;

export type TriggerType = typeof TRIGGER_TYPE[keyof typeof TRIGGER_TYPE];
