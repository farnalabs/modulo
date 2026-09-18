/**
 * Curated product analytics event types.
 *
 * These are the only frontend events captured by useProductAnalytics.
 * The list is intentionally small (~12 events) and low-volume.
 *
 * NOTE: `page_view` is NOT an event — it is a server-side daily counter
 * (Redis INCR, flushed once per dump window), consent-gated server-side
 * before increment. No per-route data is captured.
 */
export const ANALYTICS_EVENTS = {
  PIPELINE_RUN_STARTED: 'pipeline_run_started',
  PIPELINE_CREATED: 'pipeline_created',
  PIPELINE_GRAPH_SAVED: 'pipeline_graph_saved',
  HITL_GATE_CLAIMED: 'hitl_gate_claimed',
  HITL_GATE_APPROVED: 'hitl_gate_approved',
  HITL_GATE_REJECTED: 'hitl_gate_rejected',
  GUARDRAIL_OVERRIDDEN: 'guardrail_overridden',
  SCHEMA_CREATED: 'schema_created',
  CONNECTOR_ADDED: 'connector_added',
  MODEL_BACKEND_ADDED: 'model_backend_added',
  TRIGGER_CREATED: 'trigger_created',
  VARIANT_BATCH_FIRED: 'variant_batch_fired',
  EVAL_CREATED: 'eval_created',
  API_ERROR: 'api_error',
} as const

export type AnalyticsEventType = (typeof ANALYTICS_EVENTS)[keyof typeof ANALYTICS_EVENTS]
