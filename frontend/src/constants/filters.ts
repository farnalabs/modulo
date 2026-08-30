/**
 * Centralized filter value constants for run statuses.
 * These match the DB CHECK constraint: status IN ('pending', 'running', 'awaiting_human', 'claimed', 'unknown', 'complete', 'failed', 'cancelled', 'eval_failed', 'stalled', 'budget_exceeded', 'router_no_match', 'cost_ceiling_exceeded', 'compensation_failed')
 * These are used across DashboardView, RunsListView, etc.
 */
export const RUN_STATUS = {
  PENDING: 'pending',
  RUNNING: 'running',
  AWAITING_HUMAN: 'awaiting_human' as const,
  CLAIMED: 'claimed',
  UNKNOWN: 'unknown' as const,
  COMPLETE: 'complete',
  FAILED: 'failed' as const,
  CANCELLED: 'cancelled',
  EVAL_FAILED: 'eval_failed',
  STALLED: 'stalled' as const,
  BUDGET_EXCEEDED: 'budget_exceeded' as const,
  COST_CEILING_EXCEEDED: 'cost_ceiling_exceeded' as const,
  ROUTER_NO_MATCH: 'router_no_match' as const,
  COMPENSATION_FAILED: 'compensation_failed' as const,
} as const;

export type RunStatus = typeof RUN_STATUS[keyof typeof RUN_STATUS];

export const TRIGGER_TYPE = {
  MANUAL: 'manual',
  WEBHOOK: 'webhook',
  CRON: 'cron',
  POLLING: 'polling',
  AGENT_SIGNAL: 'agent_signal',
  ONGOING: 'ongoing',
  CORRECTION: 'correction',
} as const;

export type TriggerType = typeof TRIGGER_TYPE[keyof typeof TRIGGER_TYPE];
