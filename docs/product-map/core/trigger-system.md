---
id: feat-core-trigger-system
prd: 8.5
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/pipelines/webhook_trigger.feature
  - backend/tests/bdd/features/pipelines/scheduling.feature
  - backend/tests/bdd/features/triggers/polling.feature
  - backend/tests/bdd/features/triggers/pause.feature
  - backend/tests/bdd/features/triggers/manual.feature
  - backend/tests/bdd/features/triggers/cron.feature
  - backend/tests/bdd/features/triggers/agent_signal.feature
  - backend/tests/bdd/features/triggers/flood_protection.feature
  - backend/tests/bdd/features/triggers/ongoing.feature
  - backend/tests/bdd/features/triggers/trigger_event_log.feature
code:
  - backend/src/modulo/api/routes/triggers.py
  - backend/src/modulo/api/routes/admin_triggers.py
  - backend/src/modulo/api/routes/webhooks.py
  - backend/src/modulo/api/routes/slack.py
  - backend/src/modulo/core/trigger_engine/__init__.py
  - backend/src/modulo/core/trigger_engine/slack_app_mention.py
  - backend/src/modulo/core/trigger_engine/polling.py
  - backend/src/modulo/core/trigger_engine/agent_signal.py
  - backend/src/modulo/core/cron_helpers.py
  - backend/src/modulo/core/saq_worker.py
  - backend/src/modulo/core/trigger_streak.py
  - backend/src/modulo/core/pipeline_engine/classify.py
depends-on: [feat-connectors-hub, feat-core-pipeline-execution]
unit-tests:
  - backend/tests/unit/trigger_engine/test_trigger_engine.py
  - backend/tests/unit/trigger_engine/test_agent_signal.py
  - backend/tests/unit/trigger_engine/test_polling.py
  - backend/tests/unit/trigger_engine/test_polling_connector_drift.py
  - backend/tests/unit/trigger_engine/test_slack_app_mention.py
  - backend/tests/unit/api/test_triggers_endpoint.py
  - backend/tests/unit/api/test_slack_trigger_endpoint.py
  - backend/tests/bdd/steps/test_cron_triggers.py
  - backend/tests/bdd/steps/test_agent_signal.py
  - backend/tests/bdd/steps/test_ongoing_triggers.py
  - backend/tests/bdd/steps/test_polling_triggers.py
  - backend/tests/unit/api/test_admin_triggers.py
  - backend/tests/unit/api/test_webhooks_endpoint.py
  - backend/tests/unit/api/test_webhook_replay.py
  - backend/tests/unit/api/test_error_handling.py
  - backend/tests/unit/mcp/test_get_trigger_events.py
  - backend/tests/unit/mcp/test_trigger_crud_tools.py
  - backend/tests/unit/mcp/test_trigger_mgmt_tools.py
  - backend/tests/unit/cleanup_jobs/test_webhook_dedup_cleanup.py
  - backend/tests/unit/api/test_webhooks_endpoint.py
  - backend/tests/integration/test_org_trigger_pause.py
  - backend/tests/integration/saq/test_fire_due_triggers.py
  - backend/tests/bdd/steps/test_org_pause.py
  - backend/tests/unit/core/test_trigger_validation.py
  - backend/tests/unit/core/test_trigger_streak_engine.py
  - backend/tests/integration/test_trigger_streak_engine_sql.py
status: partial
---

# Core Trigger System

The trigger system allows pipelines to be initiated by five trigger types:
**Manual** (user-initiated), **Webhook** (HMAC-authenticated HTTP POST),
**Cron** (scheduled via cron expressions), **Polling** (connector-driven
condition evaluation), and **Agent Signal** (cross-pipeline node-completion
notification). All triggers share common infrastructure: a unified
`triggers` DB table, `TriggerEvent` audit log, RLS org isolation, and
concurrency management via `max_concurrent_runs`.

## Behaviours

### Common — All Trigger Types

- [x] Triggers are scoped by `organisation_id` via RLS — all queries enforce org boundary
- [x] Each trigger has an `active` boolean toggle — inactive triggers are skipped without firing
- [x] `max_concurrent_runs` limits active pipeline runs per trigger — checked before firing
- [x] Trigger CRUD (create, read, update, delete) via REST API at `/api/v1/triggers`
- [x] Triggers are associated with a `pipeline_id` — a trigger belongs to exactly one pipeline
- [x] `TriggerEvent` row created for every fire attempt regardless of outcome
- [x] `trigger_type` recorded on TriggerEvent for auditability (`manual`, `webhook`, `cron`, `polling`, `agent_signal`)
- [x] List triggers with optional `pipeline_id` and `trigger_type` filters, paginated
- [x] List trigger events with cursor-based pagination (`createdAt_eventId`)
- [x] Toggle trigger active state via `POST /triggers/{id}/toggle`
- [x] Test trigger via `POST /triggers/{id}/test` — fires a TriggerEvent and optionally creates a Run (manual type only)
- [x] Delete trigger cascades to TriggerEvent rows
- [x] `daily_spend_limit` accepted on `TriggerCreate`/`TriggerUpdate` (validated `ge=0`; explicit `null` clears it) and echoed on every trigger response (`list_triggers`, `create_trigger`, `update_trigger`, `restore_trigger`, `list_pipeline_triggers`)
- [x] MCP `create_trigger` tool accepts `max_concurrent_runs` and `daily_spend_limit` (validated) and echoes both in the response
- [x] MCP trigger CRUD: `get_trigger` (read by ID), `update_trigger` (active, max_concurrent_runs, cron config for cron triggers only, daily_spend_limit set/clear, config_json), and `delete_trigger` (soft-delete) mirror the REST `/api/v1/triggers/{id}` endpoints

### Manual Trigger

- [x] Manual trigger fires a Run immediately via `POST /triggers/{id}/test`
- [x] Pipeline snapshot created from live graph before run creation
- [x] `trigger_type='manual'` recorded on Run and TriggerEvent
- [x] Input payload passed through from test request body
- [x] `created_by` (account_id) recorded on trigger creation
- [x] Run created via `create_run()` with snapshot, pipeline, and trigger references

### Webhook Trigger

- [x] HMAC-SHA256 authentication via `X-Modulo-Webhook-Secret` header — computed over `timestamp.body`
- [x] `X-Modulo-Timestamp` header required (Unix seconds) — validated within ±300s replay window
- [x] Triggers with no `hmac_secret` accept unauthenticated requests
- [x] Deduplication via `WebhookDedupHash` — SHA256 payload hash, 5-minute TTL, unique constraint handles races
- [x] Flood protection: active run count checked against `trigger.max_concurrent_runs` — returns 429 when exceeded
- [x] Payload mapping: dot-notation paths in `config_json.payload_mapping` map raw fields to `input_payload`
- [x] Raw payload stored in `WebhookPayload` for replay — expires after dedup TTL + 1 hour
- [x] All validation outcomes logged as TriggerEvent with `trigger_type='webhook'`
- [x] Background execution via `PipelineExecutor` and `BackgroundTasks` — route returns 202 immediately
- [x] Snapshot created from live graph before each webhook run
- [x] Replay endpoint `POST /triggers/{id}/webhook/replay/{event_id}` — re-fires from stored payload, skips HMAC+timestamp validation
- [x] Replay preserves flood protection (active-run count) but bypasses deduplication — re-fires are intentional (verified: `replay_event` — "No dedup check for replays")
- [x] `TriggerNotFoundError` → 404, `TriggerInactiveError` → 404 (masked), `TimestampExpiredError` → 400, `HmacValidationError` → 401, `DuplicateWebhookError` → 400, `ConcurrentRunLimitError` → 429
- [x] Trigger loaded with `FOR UPDATE` lock to serialise concurrent webhook requests
- [x] Cleanup job at `POST /cleanup-expired` — deletes expired `WebhookDedupHash` and `WebhookPayload` rows, uses Postgres advisory lock (key=20250601)

### Cron Trigger

- [x] `fire_due_triggers` SAQ system cron
- [x] Tick interval: 30 seconds (`max_interval`)
- [x] `DatabaseCronEntry` created per matching trigger row
- [x] Stale entries removed from in-memory schedule when DB rows are deleted/deactivated
- [x] `fire_cron_trigger` SAQ per-item fire job with
- [x] Trigger re-read with `FOR UPDATE` lock to serialise concurrent fire attempts
- [x] Concurrency check against `max_concurrent_runs` before firing
- [x] Daily spend limit check via `trigger.daily_spend_limit` — sums `Run.total_cost_usd` for today, skips if limit reached
- [x] `cron_expression` validated via croniter on create/update — returns 422 on invalid expression
- [x] `next_fire_at` computed via `compute_next_fire()` (croniter get_next) and persisted
- [x] `last_fired_at` and `next_fire_at` updated after each fire
- [x] Timezone support via `cron_timezone` column — validated against `zoneinfo.ZoneInfo`
- [x] `input_template` from `config_json.input_template` used as run input payload
- [x] Preview endpoint `GET /triggers/{id}/cron/preview?count=N` — returns next N fire times (no side effects)
- [x] Inactive toggle respected: inactive triggers skipped without firing
- [x] RLS org isolation via `set_config('app.organisation_id', ...)`

### Polling Trigger

- [x] `fire_due_triggers` SAQ system cron (polling fire jobs)
- [x] `DatabasePollingEntry` created per matching trigger row
- [x] `fire_polling_trigger` SAQ per-item fire job with `autoretry_for=(ConnectionError, TimeoutError, OSError)`, `max_retries=2`, `default_retry_delay=30`
- [x] Trigger re-read with `FOR UPDATE` lock for concurrency serialisation
- [x] Next-fire guard: if `next_fire_at > now()` the task returns `already_fired_this_cycle` without firing
- [x] `schedule_polling_trigger()` in TriggerEngine computes `next_fire_at` from `poll_interval_seconds`
- [x] Daily spend limit (`trigger.daily_spend_limit`) enforced before run creation — `spend_limit_reached` TriggerEvent, `next_fire_at` advanced, `skipped` returned
- [x] Connector instance loaded from DB by `connector_instance_id` in `config_json`
- [x] Credentials decrypted via Fernet-backed secrets backend
- [x] One-shot connector built via `_build_polling_connector()` (filesystem, github, gitlab, linear, jira, slack)
- [x] Poll query executed via `connector.query(ConnectorQuery(resource=poll_query))`
- [x] JMESPath `condition_expression` evaluated against query result records
- [x] Condition met → run created with `input_payload` containing `records`, `total`, `poll_query`
- [x] Condition not met → `no_match` logged, `next_fire_at` updated regardless
- [x] `snapshot_id` resolved from trigger config — falls back to `uuid.UUID(int=0)` if unset or invalid
- [x] Stale trigger entries removed from in-memory schedule when DB rows are deleted/deactivated
- [x] Active/inactive toggle respected
- [x] RLS org isolation on all DB queries

### Agent Signal Trigger

- [x] `fire_agent_signal()` called when a source pipeline's node completes execution
- [x] Queries active triggers where `trigger_type='agent_signal'` matching org
- [x] Filters by `config_json.source_pipeline_id` and `config_json.source_node_id`
- [x] Concurrency check against `max_concurrent_runs` — skips with `concurrency_limit_reached` event
- [x] Input payload built from: `source_run_id`, `source_pipeline_id`, `source_node_id`, optional `node_output`
- [x] Snapshot ID resolved from `config_json.snapshot_id` — falls back to `uuid.uuid4()` if unset or invalid
- [x] Child pipeline run created with `parent_run_id` linking back to source run
- [x] Multiple agent_signal triggers can fire from a single node completion (org-scoped query returns all matching triggers)
- [x] TriggerEvent logged with `trigger_type='agent_signal'` and result `signal_fired`

### Ongoing Trigger

- [x] `trigger_type='ongoing'` keeps a pipeline topped up to a target number of in-flight runs
- [x] `PATCH /triggers/{id}/ongoing` validates config via the shared `validate_ongoing_config` helper (`daily_spend_limit` required > 0, `target` in 1..20, `scan_interval_seconds` >= 60 tick)
- [x] `max_concurrent_runs` treated as the target upper bound (validated 1..20 via `ck_triggers_ongoing_target_range`)
- [x] In-flight count (`_count_ongoing_runs`) includes pending runs toward the target
- [x] Below target → top-up creates runs referencing the ongoing trigger
- [x] At/above target → no top-up
- [x] Daily spend limit respected before run creation
- [x] Org pause respected — no top-up on a paused org
- [x] `in_flight` surfaced on trigger detail/list responses for ongoing triggers
- [x] **No-delivery auto-deactivation (FAR-190)**: an ongoing trigger is auto-deactivated after N consecutive `no_delivery` terminal runs (default N=5, per-trigger `max_no_delivery_streak`; 24h wall-clock window, per-trigger `no_delivery_min_window_hours`) by the DB-backed streak engine (`core/trigger_streak.py`, sweep wired into `dispatcher_reconcile` every 60s) — a silently-dry daemon no longer runs forever
- [x] Streak walk is fail-closed: stops at the first `delivered`/`excluded`/`unclassified` terminal run or any terminal run with no classification record; boundary = `GREATEST(last_delivery_at, streak_epoch)`; cancelled/budget-exceeded runs are excluded AND break the walk
- [x] Deactivation is a guarded atomic `UPDATE ... WHERE active AND <streak folded into WHERE>` (no TOCTOU; concurrent ticks are a no-op) with AuditEvent + TriggerEvent lifecycle records + notifier dispatch; per-org cap 10/h, mass-cascade alert at 5/24h, kill switch `MODULO_STREAK_DEACTIVATE_KILL_SWITCH`
- [x] `streak_epoch` (migration 0102) re-anchored on every `active=True` transition (create/update/toggle/restore/re-enable + circuit-breaker reset) so a re-enabled trigger's streak restarts; config-failure deactivation (FAR-158, 5 consecutive `no_pipeline` failures via Redis) and the no-delivery deactivation share the `record_ongoing_deactivation_lifecycle` audit ceremony

### Org-wide Pause (Kill-Switch)

- [x] Org-wide pause toggle at `PUT /api/v1/admin/orgs/{org_id}/triggers/pause` — admin-only (`org.triggers.pause.manage`), `kill_switch_eligible=False` so the authz kill-switch cannot lift it
- [x] Idempotent toggle: re-PUTting the current state writes no audit event
- [x] Toggle audited as `triggers_paused` (payload `{paused: bool}`) with fail-open-with-alert audit (toggle always commits; audit failures loudly logged)
- [x] `create_run` is the SINGLE authority gate: blocks NEW trigger-initiated runs (webhook/replay/cron/polling/agent_signal) when the org is paused; manual, `test_trigger`, feedback correction, and variant runs pass
- [x] Paused webhook/replay delivery → HTTP 202 `{"status":"paused"}` with NO `run_id` and exactly one committed `TriggerEvent` with `validation_result='paused'` (skipped entirely if the org row was HARD-deleted — an orphan trigger insert would violate the organisations FK)
- [x] Cron/polling fire jobs on a paused org → `{"status":"skipped","reason":"triggers_paused"}` (early check + `create_run` race backstop; no paused event from the fire path)
- [x] Agent signal on a paused org → exactly one `paused` TriggerEvent, result `skipped/triggers_paused`
- [x] `fire_due_triggers` SKIP-not-defer: cron/polling enqueue skipped for paused orgs while `next_fire_at` still advances; `cron_skipped_paused`/`polling_skipped_paused` counters; scheduled reports still enqueue
- [x] `list_triggers` returns top-level `triggers_paused` + `paused_at` reflecting the SAME predicate as the gate (`org_row_is_paused` = `triggers_paused` column OR non-active org `status`) — a suspended/deleted org shows the paused banner
- [x] Pause read failure handling: scheduler batched read and per-item fire jobs degrade to not-paused on a pre-migration `ProgrammingError` (inside a savepoint for per-item jobs — transaction never poisoned); any other `SQLAlchemyError` RE-RAISES so the tick/job fails and SAQ retries — never fabricate "paused" on a DB error
- [x] Migration 0069 adds `organisations.triggers_paused`/`triggers_paused_at` + CHECK and widens `ck_trigger_events_validation_result` to the full 19-value vocabulary (dialect-guarded: Postgres `NOT VALID`/`VALIDATE`; SQLite via Alembic batch mode)
- [x] Validation-result vocabulary extended to 19 values (adds `event_type_not_accepted`, `spend_limit_reached`, `no_pipeline`, `test`, `paused`) — fixes pre-existing IntegrityError on those writes

## Edge Cases

- [x] `next_fire_at` is `None` → cron/polling schedulers skip without firing (both fire paths filter `Trigger.next_fire_at.isnot(None)` — verified in `cron_helpers.py` cron + polling candidate reads)
- [x] `cron_expression` is `None` on a cron trigger → preview endpoint returns 400 "Trigger has no cron expression configured" (test: `test_preview_cron_schedule_no_expression_returns_400`)
- [x] Webhook replay bypasses deduplication by design — an intentional re-fire never re-checks the dedup hash (verified: `replay_event` in `trigger_engine/__init__.py` — "No dedup check for replays"); flood protection (active-run count) IS still enforced on replay
- [x] Webhook with `hmac_secret=None` — authentication skipped entirely (route validates only when `cfg.get("hmac_secret") is not None`; HMAC-less triggers accept unauthenticated deliveries by design)
- [x] Webhook X-Modulo-Timestamp is malformed (not an integer) → `TimestampExpiredError` → 400 (verified: `verify_timestamp` raises on `ValueError`/`TypeError`)
- [x] Webhook body is not a JSON object → 400 "Request body must be a JSON object" (test: `test_receive_webhook_missing_json_body_returns_400`)
- [x] Polling trigger with `connector_instance_id=None` in config → `poll_error` event logged, trigger skipped (verified: `fire_polling_trigger` — "Polling trigger missing connector_instance_id in config_json")
- [x] Polling trigger with unset/invalid `snapshot_id` → falls back to `uuid.UUID(int=0)` (tests: `test_invalid_snapshot_id_falls_back_to_zero_uuid`, `test_missing_snapshot_id_falls_back_to_zero_uuid`)
- [x] Polling trigger with missing connector instance in DB → `poll_error` event logged (test: `test_connector_not_found_logs_warning`)
- [x] Polling connector init fails (bad creds, unsupported type) → `poll_error` event logged (test: `test_connector_init_failed_returns_error`)
- [x] Poll query execution fails → `poll_error` event logged (tests: `test_query_failed_returns_error`, `test_query_timeout_returns_error`)
- [x] Invalid JMESPath condition expression → `poll_error` event logged with expression detail (tests: `test_invalid_jmespath_expression`, `test_condition_eval_failed_returns_error`)
- [x] Agent signal with `source_pipeline_id` that doesn't match any trigger → silent skip (no results) (tests: `test_no_matching_triggers_returns_empty`, `test_skips_non_matching_source_pipeline`)
- [x] Agent signal trigger for a non-existent node_id → all triggers evaluated but none match → empty results (test: `test_skips_non_matching_node_id`)
- [x] Cron trigger with invalid timezone → validation returns error string (`validate_cron_expression` → "Invalid timezone: ..."), API returns 422 via `_validated_next_fire` (test added 2026-08-15 in `test_error_handling.py` — validator unit test + route 422 test)
- [x] Cron trigger with `daily_spend_limit=0` → all runs blocked by spend check (0 >= 0)
- [x] Toggling a deleted trigger → 404 Not Found (toggle query filters `Trigger.deleted_at.is_(None)`)
- [x] Deleting a trigger that has TriggerEvent rows → cascade delete (TriggerEvent.trigger_id FK `ondelete="CASCADE"` in `db/models/trigger_event.py`)
- [x] Cursor parsing in list_trigger_events: malformed cursor (no `_` separator) → logged as warning, treated as no cursor
- [x] `FOR UPDATE` lock / advisory lock on the trigger row prevents concurrent webhook/polling/cron fires for the same trigger — serialises to one at a time (webhook `FOR UPDATE`, polling `pg_try_advisory_lock` — test: `test_lock_not_acquired_returns_trigger_busy`)
- [x] Empty `page_size` in list_triggers → defaults to 20, clamped to [1, 100] via `Query(20, ge=1, le=100)` (validation tests added 2026-08-15 in `test_error_handling.py`)
- [x] Page < 1 → FastAPI validation returns 422 (test: `test_list_triggers_page_below_one_returns_422`)
- [x] `limit` in list_trigger_events clamped to [1, 100] via `Query(20, ge=1, le=100)` (tests: `test_list_trigger_events_limit_above_100_returns_422`, `test_list_trigger_events_limit_below_one_returns_422`)

## Error Handling

- [x] Webhook triggers: typed exceptions mapped to specific HTTP statuses (404/400/401/429)
- [x] `list_triggers` catches `ProgrammingError` → 501 and `SQLAlchemyError` → 503
- [x] `update_cron_config` catches `ProgrammingError` → 501 and `SQLAlchemyError` → 503
- [x] `preview_cron_schedule` catches `ProgrammingError` → 501 and `SQLAlchemyError` → 503
- [x] `update_polling_config` catches `ProgrammingError` → 501 and `SQLAlchemyError` → 503
- [x] `test_polling_condition` catches `ProgrammingError` → 501 and `SQLAlchemyError` → 503
- [x] `create_trigger` catches `ProgrammingError` → 501 and `SQLAlchemyError` → 503
- [x] `update_trigger` catches `ProgrammingError` → 501 and `SQLAlchemyError` → 503
- [x] `delete_trigger` catches `ProgrammingError` → 501 and `SQLAlchemyError` → 503
- [x] `toggle_trigger` catches `ProgrammingError` → 501 and `SQLAlchemyError` → 503
- [x] `test_trigger` catches `ProgrammingError` → 501 and `SQLAlchemyError` → 503
- [x] `list_trigger_events` (triggers.py) catches `ProgrammingError` → 501 and `SQLAlchemyError` → 503
- [x] `list_pipeline_triggers` catches `ProgrammingError` → 501 and `SQLAlchemyError` → 503
- [x] Admin `list_trigger_events` catches `ProgrammingError` → 501 and `SQLAlchemyError` → 503 (both main query and count query)
- [x] `receive_webhook` catches `ProgrammingError` → 501 and `SQLAlchemyError` → 503
- [x] `replay_webhook` catches `ProgrammingError` → 501 and `SQLAlchemyError` → 503
- [x] `cleanup_expired` distinguishes `ProgrammingError` (→ 501), `SQLAlchemyError` (→ 503), and other exceptions (→ 500)
- [x] `list_trigger_events` cursor parsing logs warning on malformed cursor instead of silent `pass`
- [x] All 15 trigger route handlers catch `Exception` → 500 with `except HTTPException: raise` guard and `_log.exception`

### Resilience & Integration Robustness

- [x] All DB routes catch `ProgrammingError` → 501 with migration hint
- [x] All DB routes catch `SQLAlchemyError` → 503 with retry hint
- [ ] No retry/backoff on database connection failures at route level (routes return 501/503 immediately; retries live in the SAQ scheduler jobs, not the HTTP layer — a genuine product gap for transient DB blips on webhook delivery)
- [x] Webhook dedup cleanup uses advisory lock — safe across workers (`cleanup_expired_dedup_hashes` acquires `pg_try_advisory_xact_lock(20250601)`; verified in `trigger_engine/__init__.py`)
- [x] Cron scheduler retries on Exception (autoretry_for, max_retries=3)
- [x] Polling scheduler retries on Exception (autoretry_for, max_retries=2)
- [x] Webhook flood protection uses FOR UPDATE lock — serialises per trigger
- [ ] No circuit breaker on repeat DB failures (a persistently failing DB keeps failing each request; no trip-and-cool-down layer at route level)

## Known Gaps

- BDD feature file `webhook_trigger.feature` has 5 scenarios all tagged `@awaiting-implementation` — no executable BDD coverage exists for webhook triggers (the step definitions exist in `steps/test_pipelines.py` but reference a hardcoded 2023 `X-Modulo-Timestamp` outside the ±300s replay window, so they 400 instead of passing; the route's own timestamp guard fires before the mocked engine is reached)
- BDD `scheduling.feature` has 5 cron scenarios but zero polling scenarios — executable polling BDD lives in `triggers/polling.feature` (9 scenarios, wired)
- `_build_polling_connector()` is a standalone copy of `connector_hub._build_connector()` — drifts as connector hub gains new types (41+ types registered vs 6 in polling)
- (Resolved) Agent signal triggers had no BDD or unit test coverage for the `fire_agent_signal()` function — now covered by `triggers/agent_signal.feature` (9 scenarios, wired via `steps/test_agent_signal.py`) and `unit/trigger_engine/test_agent_signal.py`
- (Resolved) Ongoing trigger config validation had zero direct coverage — now covered by `unit/core/test_trigger_validation.py` (39 tests) plus `triggers/ongoing.feature` (6 scenarios, wired via `steps/test_ongoing_triggers.py`)
- `list_trigger_events` in `triggers.py` uses a separate count query — not DRY with admin version
- `snapshot_id` falls back to `uuid.UUID(int=0)` in polling/agent_signal/cron — may create runs against latest snapshot instead of intended one
- Polling trigger has no `retain_payload` equivalent (webhook has it for replay)
- `max_concurrent_runs` uses pipeline-level active-run counting; PRD 8.5 suggests trigger-level counting
- (Resolved) Trigger-level `daily_spend_limit` is enforced at fire time by both cron and polling but was not exposed via the trigger CRUD/polling-config API — resolved 2026-08-02: accepted on `TriggerCreate`/`TriggerUpdate`/`PollingConfigUpdate` and echoed on all trigger responses
- (Resolved) No unit tests for `admin_triggers.py` ProgrammingError → 501 path — covered in test_admin_triggers.py
- (Resolved) No unit tests for generic Exception→500 on trigger routes — covered in test_admin_triggers.py
- (Resolved 2026-08-15) No unit tests for `webhooks.py` ProgrammingError → 501 path — deleted in the 530-test reduction; re-added as parametrized SESSION_CASES in `tests/unit/api/test_error_handling.py` (receive + replay ProgrammingError→501 / SQLAlchemyError→503; cleanup-expired is require_permission-gated and intentionally excluded)

## QA History

- 2026-08-15 (dist/partial-core2): Behaviour-verification pass on the trigger system — drove the Edge Cases section from 8/23 to 21/23 checked and clarified the Resilience section.
  - Marked [x] with code/test verification: `next_fire_at` None skip (cron + polling `isnot(None)` filters), cron-expression-None → 400 preview, HMAC-less webhook auth skip, malformed timestamp → 400, polling `connector_instance_id=None` → `poll_error`, missing connector / init-fail / query-fail / invalid-JMESPath → `poll_error`, agent-signal silent-skip / non-existent-node → empty, invalid cron timezone → 422, toggle-deleted → 404, TriggerEvent cascade delete (`ondelete="CASCADE"`), FOR-UPDATE/advisory-lock serialisation, pagination bounds (empty `page_size` → 20, page<1 → 422, `limit` clamp [1,100]), and webhook-dedup-cleanup advisory lock (key 20250601).
  - **Corrected a stale claim**: replay does NOT re-check dedup — `replay_event` explicitly bypasses dedup ("No dedup check for replays — this is an intentional re-fire") while still enforcing flood protection. Rewrote the Edge Case + behaviour lines to match reality.
  - **RESOLVED** the Known Gap "No unit tests for `webhooks.py` ProgrammingError → 501 path" — added receive/replay ProgrammingError→501 + SQLAlchemyError→503 parametrized SESSION_CASES in `tests/unit/api/test_error_handling.py` (cleanup-expired excluded: it is `require_permission`-gated so the kill-switch read fail-closes before the handler).
  - **Added new tests**: 6 pagination/validation tests + 2 cron-timezone validation tests + 4 webhook error-path cases in `test_error_handling.py` (all pass).
  - Left unchecked (genuine gaps): route-level DB retry/backoff and a DB-failure circuit breaker — both are real product gaps, noted inline.
  - BDD: `webhook_trigger.feature`'s 5 `@awaiting-implementation` scenarios remain tagged — verified they fail because `steps/test_pipelines.py` hardcodes a 2023 `X-Modulo-Timestamp` outside the ±300s window (the route's real timestamp guard fires before the mocked engine). Not enableable without touching that step file.
  - Status: partial (webhook BDD, `_build_polling_connector` drift, snapshot fallback semantics, pipeline-level vs trigger-level `max_concurrent_runs` remain).
- 2026-08-13 (improve-tests): QA lens pass on the shared `ongoing` trigger validator (`modulo.core.trigger_validation.validate_ongoing_config`, FAR-158) — added a dedicated 39-test unit suite (`tests/unit/core/test_trigger_validation.py`; previously zero direct coverage). Locks the pure validator contract that every write surface (REST `create_trigger`/`update_trigger`, MCP `create_trigger`/`update_trigger`, `PATCH /triggers/{id}/ongoing`) shares: non-`ongoing` types pass through untouched, `daily_spend_limit` required and > 0 (None/0/negative/Decimal), target range 1..20 (boundaries and out-of-range), target ≤ pipeline `max_concurrent_runs` cap (reject above, accept at/below), `scan_interval_seconds` ≥ 60 tick (default 60 when absent or falsy, numeric-string coercion, below-tick rejection), rule ordering (spend-limit reported before target range, target range before pipeline cap), and non-numeric `daily_spend_limit`/`scan_interval_seconds` values raising the same 422 as every other rule (previously leaked `TypeError`/`ValueError` → 500).
- 2026-08-04: Added the org-wide "pause all pipeline triggers" kill-switch. New `PUT /api/v1/admin/orgs/{org_id}/triggers/pause` admin endpoint, `create_run` authority gate, paused webhook contract (202 `{"status":"paused"}` + committed `paused` TriggerEvent), cron/polling SKIP-not-defer skip-with-advance, agent-signal pause event, `list_triggers` top-level `triggers_paused`/`paused_at`, and migration 0069 (new org columns + widened 19-value `ck_trigger_events_validation_result` — fixing a pre-existing IntegrityError on `event_type_not_accepted`/`spend_limit_reached`/`no_pipeline`/`test` writes). New BDD feature `triggers/pause.feature`, integration `test_org_trigger_pause.py`, and saq pause test. Behaviours marked [x] against code.
- 2026-08-02 (round 2): improve-architecture: RESOLVED the Known Gap "Trigger-level `daily_spend_limit` is enforced at fire time by both cron and polling but not exposed via the trigger CRUD/polling-config API". `daily_spend_limit` is now accepted on `TriggerCreate`, `TriggerUpdate`, and `PollingConfigUpdate` (validated `ge=0`; explicit `null` clears via `model_fields_set`, omitted `None` leaves unchanged) and echoed on every trigger response (`list_triggers`, `create_trigger`, `update_trigger`, `restore_trigger`, `update_polling_config`, `list_pipeline_triggers`). MCP `create_trigger` now also accepts `max_concurrent_runs` + `daily_spend_limit`. Marked "Cron trigger with `daily_spend_limit=0` → all runs blocked" [x] (verified `0 >= 0` short-circuit in `cron_helpers.py`). Added 7 unit tests in `test_triggers_endpoint.py`.
- 2026-08-02: improve-architecture: RESOLVED the "Daily spend limit applies to cron triggers only — polling has no spend limit check" known gap. `fire_polling_trigger` now enforces `trigger.daily_spend_limit` (new `_daily_spend_limit_reached()` helper) — over-budget triggers log a `spend_limit_reached` TriggerEvent, advance `next_fire_at`, and skip. Wired up the previously-orphaned `triggers/polling.feature` (9 executable fire-path scenarios + 3 spend-limit scenarios) via `tests/bdd/steps/test_polling_triggers.py` and added 5 unit tests (`TestDailySpendLimit`). Updated frontmatter (`bdd:`/`unit-tests:`).
- 2026-07-05: Cross-cutting QA (index 169): Added `SQLAlchemyError` catch → 503 to all 16 trigger route handlers (triggers.py: 12, admin_triggers.py: 1 route with 2 try/except blocks, webhooks.py: 3). Fixed silent cursor-parsing error swallowing in admin_triggers.py (now logs warning). Added test_trigger_sqlalchemy_error.py with 18 tests covering SQLAlchemyError→503 for all trigger route handlers. Updated product map: marked all 50+ previously unchecked behaviours as [x] (verified against code implementation), added Error Handling checkbox for SQLAlchemyError→503, added Resilience & Integration Robustness section (8 checkboxes: 4 [x] + 4 [ ]). All existing unit tests continue to pass. Status: partial.
- 2026-07-09: Cross-cutting QA (index 287): Fixed CRITICAL — added `except Exception → 500` with `except HTTPException: raise` guard and `_log.exception` to 14 trigger route handlers (12 in triggers.py, 1 in admin_triggers.py with 2 try/except blocks, 2 in webhooks.py). `cleanup_expired` already had the guard. Moved lazy `hashlib`/`json` imports to module level in test_trigger endpoint. Created test_trigger_exception_guard.py with 15 tests covering Exception→500 on all routes (12 triggers.py + admin_triggers.py + 2 webhooks.py). Removed 3 resolved Known Gaps. Updated product map Error Handling section. All tests pass. Merged to main. Status: partial.
- 2026-07-12: Round 3 QA (improve-architecture batch 3): Fixed MINOR — added `exc_info=True` to `_log.warning()` in cursor decode except blocks in triggers.py and admin_triggers.py (both caught (ValueError, AttributeError) for malformed cursor but didn't log the exception traceback). B904 audit: all except blocks across triggers.py, admin_triggers.py, and webhooks.py already use `from None`/`from exc` correctly. CancelledError guard: not applicable (Python 3.12+). No stale frontmatter or resolved known gaps in active gaps section.
- 2026-07-31: improve-architecture: Fixed MINOR — removed duplicated `@router.get` decorator on `admin_triggers.py.list_trigger_events` that double-registered the route (inner registration served the raw handler, making `handle_db_errors` dead code). Same cross-cutting fix applied to `admin_monitor_config.py` GET/PUT (see feat-observability-monitoring-config). Cleaned up stale frontmatter refs to test files deleted in the test-reduction commits (#102/#109): re-pointed cron BDD to `tests/bdd/steps/test_cron_triggers.py`, removed dead refs to `test_trigger_programming_error.py`/`test_trigger_exception_guard.py`, and restored error-path coverage (501/503/500) for `list_trigger_events` in test_admin_triggers.py (3 new tests).
