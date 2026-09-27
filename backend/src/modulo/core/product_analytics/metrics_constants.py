"""Product analytics ingest constants."""

MAX_BATCH_SIZE = 1000
API_ERROR_DAILY_CAP = 100

VALID_EVENT_TYPES: set[str] = {
    "pipeline_run_started",
    "pipeline_created",
    "pipeline_graph_saved",
    "run.autonomy_level_applied",
    "run.autonomy_recommendation_clamped",
    "hitl_review_claimed",
    "hitl_review_approved",
    "hitl_review_rejected",
    "guardrail_overridden",
    "schema_created",
    "connector_added",
    "model_backend_added",
    "trigger_created",
    "variant_batch_fired",
    "eval_created",
    "api_error",
}
