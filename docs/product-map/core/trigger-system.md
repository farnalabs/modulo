---
id: feat-core-trigger-system
prd: 8.5
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/pipelines/webhook_trigger.feature
  - backend/tests/bdd/features/pipelines/scheduling.feature
code:
  - backend/src/modulo/api/routes/triggers.py
  - backend/src/modulo/api/routes/admin_triggers.py
  - backend/src/modulo/api/routes/webhooks.py
  - backend/src/modulo/core/trigger_engine/__init__.py
  - backend/src/modulo/core/trigger_engine/polling.py
  - backend/src/modulo/core/trigger_engine/agent_signal.py
  - backend/src/modulo/core/cron_scheduler.py
depends-on: [feat-connectors-hub, feat-core-pipeline-execution]
unit-tests:
  - backend/tests/unit/trigger_engine/test_trigger_engine.py
  - backend/tests/unit/trigger_engine/test_polling.py
  - backend/tests/unit/trigger_engine/test_polling_connector_drift.py
  - backend/tests/unit/api/test_triggers_endpoint.py
  - backend/tests/unit/api/test_cron_triggers_bdd.py
  - backend/tests/unit/api/test_admin_triggers.py
  - backend/tests/unit/api/test_webhooks_endpoint.py
  - backend/tests/unit/api/test_webhook_replay.py
  - backend/tests/unit/api/test_trigger_programming_error.py
  - backend/tests/unit/mcp/test_get_trigger_events.py
  - backend/tests/unit/cleanup_jobs/test_webhook_dedup_cleanup.py
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
- [x] Replay preserves dedup and flood protection checks
- [x] `TriggerNotFoundError` → 404, `TriggerInactiveError` → 404 (masked), `TimestampExpiredError` → 400, `HmacValidationError` → 401, `DuplicateWebhookError` → 400, `ConcurrentRunLimitError` → 429
- [x] Trigger loaded with `FOR UPDATE` lock to serialise concurrent webhook requests
- [x] Cleanup job at `POST /cleanup-expired` — deletes expired `WebhookDedupHash` and `WebhookPayload` rows, uses Postgres advisory lock (key=20250601)

### Cron Trigger

- [x] `DatabaseCronScheduler` — custom Celery beat scheduler querying `triggers` where `trigger_type='cron'`, `active=true`, `next_fire_at <= now()`
- [x] Tick interval: 30 seconds (`max_interval`)
- [x] `DatabaseCronEntry` created per matching trigger row
- [x] Stale entries removed from in-memory schedule when DB rows are deleted/deactivated
- [x] `CronFireTask` — Celery task with `autoretry_for=(Exception,)`, `max_retries=3`, `default_retry_delay=60`
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

- [x] `DatabasePollingScheduler` — Celery beat scheduler querying `triggers` where `trigger_type='polling'`, `active=true`, `next_fire_at <= now()`
- [x] `DatabasePollingEntry` created per matching trigger row
- [x] `PollingFireTask` with `autoretry_for=(Exception,)`, `max_retries=2`, `default_retry_delay=30`
- [x] Trigger re-read with `FOR UPDATE` lock for concurrency serialisation
- [x] Next-fire guard: if `next_fire_at > now()` the task returns `already_fired_this_cycle` without firing
- [x] `schedule_polling_trigger()` in TriggerEngine computes `next_fire_at` from `poll_interval_seconds`
- [x] Connector instance loaded from DB by `connector_instance_id` in `config_json`
- [x] Credentials decrypted via Fernet-backed secrets backend
- [x] One-shot connector built via `_build_polling_connector()` (filesystem, github, gitlab, linear, jira, slack)
- [x] Poll query executed via `connector.query(ConnectorQuery(resource=poll_query))`
- [x] JMESPath `condition_expression` evaluated against query result records
- [x] Condition met → run created with `input_payload` containing `records`, `total`, `poll_query`
- [x] Condition not met → `no_match` logged, `next_fire_at` updated regardless
- [x] `snapshot_id` resolved from trigger config — falls back to `uuid.uuid4()` if unset or invalid
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

## Edge Cases

- [ ] `next_fire_at` is `None` → cron/polling schedulers skip without firing (comparison `<= now()` is false)
- [ ] `cron_expression` is `None` on a cron trigger → preview endpoint returns 400 "Trigger has no cron expression configured"
- [ ] Webhook duplicate payload during replay → `DuplicateWebhookError` with 400 (replay creates new dedup hash)
- [ ] Webhook with `hmac_secret=None` — authentication skipped entirely
- [ ] Webhook X-Modulo-Timestamp is malformed (not an integer) → `TimestampExpiredError` → 400
- [ ] Webhook body is not a JSON object → 400 "Request body must be a JSON object"
- [ ] Polling trigger with `connector_instance_id=None` in config → `poll_error` event logged, trigger skipped
- [ ] Polling trigger with unset/invalid `snapshot_id` → falls back to `uuid.uuid4()` (may create run against wrong snapshot)
- [ ] Polling trigger with missing connector instance in DB → `poll_error` event logged
- [ ] Polling connector init fails (bad creds, unsupported type) → `poll_error` event logged
- [ ] Poll query execution fails → `poll_error` event logged
- [ ] Invalid JMESPath condition expression → `poll_error` event logged with expression detail
- [ ] Agent signal with `source_pipeline_id` that doesn't match any trigger → silent skip (no results)
- [ ] Agent signal trigger for a non-existent node_id → all triggers evaluated but none match → empty results
- [ ] Cron trigger with invalid timezone → validation returns error string, API returns 422
- [ ] Cron trigger with `daily_spend_limit=0` → all runs blocked by spend check (0 >= 0)
- [ ] Toggling a deleted trigger → 404 Not Found
- [ ] Deleting a trigger that has TriggerEvent rows → cascade delete (TriggerEvent FK to trigger is ON DELETE CASCADE)
- [x] Cursor parsing in list_trigger_events: malformed cursor (no `_` separator) → logged as warning, treated as no cursor
- [ ] `FOR UPDATE` lock on trigger row prevents concurrent webhook/polling/cron fires for the same trigger — serialises to one at a time
- [ ] Empty `page_size` in list_triggers → defaults to 20, clamped to [1, 100]
- [ ] Page < 1 → FastAPI validation returns 422
- [ ] `limit` in list_trigger_events clamped to [1, 100]

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

### Resilience & Integration Robustness

- [x] All DB routes catch `ProgrammingError` → 501 with migration hint
- [x] All DB routes catch `SQLAlchemyError` → 503 with retry hint
- [ ] No retry/backoff on database connection failures at route level
- [ ] Webhook dedup cleanup uses advisory lock — safe across workers
- [x] Cron scheduler retries on Exception (autoretry_for, max_retries=3)
- [x] Polling scheduler retries on Exception (autoretry_for, max_retries=2)
- [x] Webhook flood protection uses FOR UPDATE lock — serialises per trigger
- [ ] No circuit breaker on repeat DB failures

## Known Gaps

- BDD feature file `webhook_trigger.feature` has 5 scenarios all tagged `@awaiting-implementation` — no executable BDD coverage exists for webhook triggers
- BDD `scheduling.feature` has 5 cron scenarios but zero polling scenarios — no BDD coverage for polling trigger behaviour
- `_build_polling_connector()` is a standalone copy of `connector_hub._build_connector()` — drifts as connector hub gains new types (41+ types registered vs 6 in polling)
- Agent signal triggers have no BDD or unit test coverage for the `fire_agent_signal()` function
- `list_trigger_events` in `triggers.py` uses a separate count query — not DRY with admin version
- `snapshot_id` falls back to `uuid.uuid4()` in polling/agent_signal/cron — may create runs against latest snapshot instead of intended one
- Polling trigger has no `retain_payload` equivalent (webhook has it for replay)
- `max_concurrent_runs` uses pipeline-level active-run counting; PRD 8.5 suggests trigger-level counting
- Daily spend limit applies to cron triggers only — polling has no spend limit check
- No unit tests for `admin_triggers.py` ProgrammingError → 501 path
- No unit tests for `webhooks.py` ProgrammingError → 501 path
- No unit tests for SQLAlchemyError→503 existed before QA pass — now covered in test_trigger_sqlalchemy_error.py

## QA History

- 2026-07-05: Cross-cutting QA (index 169): Added `SQLAlchemyError` catch → 503 to all 16 trigger route handlers (triggers.py: 12, admin_triggers.py: 1 route with 2 try/except blocks, webhooks.py: 3). Fixed silent cursor-parsing error swallowing in admin_triggers.py (now logs warning). Added test_trigger_sqlalchemy_error.py with 18 tests covering SQLAlchemyError→503 for all trigger route handlers. Updated product map: marked all 50+ previously unchecked behaviours as [x] (verified against code implementation), added Error Handling checkbox for SQLAlchemyError→503, added Resilience & Integration Robustness section (8 checkboxes: 4 [x] + 4 [ ]). All existing unit tests continue to pass. Status: partial.
