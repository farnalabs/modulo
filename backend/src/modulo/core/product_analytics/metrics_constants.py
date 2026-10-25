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

# Payload fields are an explicit privacy boundary.  Product analytics promises
# aggregate, non-PII telemetry, but the browser supplies event payloads.  Keep
# only stable opaque identifiers and the bounded API-error fields that the
# frontend actually emits; unknown fields are dropped before staging.
EVENT_PAYLOAD_FIELDS: dict[str, frozenset[str]] = {
    "pipeline_run_started": frozenset({"run_id"}),
    "pipeline_created": frozenset({"pipeline_id"}),
    "pipeline_graph_saved": frozenset({"pipeline_id"}),
    "run.autonomy_level_applied": frozenset(),
    "run.autonomy_recommendation_clamped": frozenset(),
    "hitl_review_claimed": frozenset(),
    "hitl_review_approved": frozenset(),
    "hitl_review_rejected": frozenset(),
    "guardrail_overridden": frozenset(),
    "schema_created": frozenset({"schema_id"}),
    "connector_added": frozenset({"connector_id"}),
    "model_backend_added": frozenset({"model_backend_id"}),
    "trigger_created": frozenset(),
    "variant_batch_fired": frozenset(),
    "eval_created": frozenset({"eval_id"}),
    "api_error": frozenset({"route", "status"}),
}
