---
id: feat-core-polling-trigger
prd: §8.5
delivery-tasks: [task-nv10-polling-trigger]
bdd:
  - backend/tests/bdd/features/pipelines/scheduling.feature
code:
  - backend/src/modulo/core/trigger_engine/__init__.py
  - backend/src/modulo/core/trigger_engine/polling.py
depends-on:
  - feat-core-connector-hub
  - feat-core-trigger-entity
status: partial
---

# Polling Trigger

Discovered from 1 completed delivery tasks. Also specified in PRD §8.5 (Trigger System) as a v1 polling type using connector-based condition evaluation.

## Behaviours

### Schedule & Firing
- [x] DatabasePollingScheduler queries `triggers` table for `trigger_type='polling'` and `next_fire_at <= now()` on each beat tick
- [x] DatabasePollingEntry is created per matching trigger row
- [x] PollingFireTask fires asynchronously via Celery with `autoretry_for=(Exception,)`, `max_retries=2`, `default_retry_delay=30`
- [x] Trigger row re-read with `FOR UPDATE` lock to serialise concurrent fire attempts
- [x] `schedule_polling_trigger()` in TriggerEngine computes `next_fire_at` from `poll_interval_seconds` and persists it
- [x] `next_fire_at` and `last_fired_at` updated after each fire cycle (both on condition_met and no_match)
- [x] Next-fire guard: if `next_fire_at > now()`, task returns `already_fired_this_cycle` without firing
- [x] Stale trigger entries removed from in-memory schedule when DB rows are deleted/deactivated
- [ ] Active/inactive toggle respected: inactive triggers are skipped without logging
- [ ] RLS org isolation: all DB queries enforce `organisation_id` scoping

### Connector & Query Execution
- [x] Connector instance loaded from DB by `connector_instance_id` stored in `config_json`
- [x] Credentials decrypted via Fernet-backed secrets backend
- [x] One-shot connector built via `_build_polling_connector()` (supports: filesystem, github, gitlab, linear, jira, slack)
- [x] Poll query executed via `connector.query(ConnectorQuery(resource=poll_query))`
- [x] Connector instance not found -> `poll_error` logged
- [x] Connector init fails (bad creds, unsupported type) -> `poll_error` logged
- [x] Poll query execution fails -> `poll_error` logged

### Condition Evaluation
- [x] JMESPath `condition_expression` evaluated against query result records
- [x] Empty/null `condition_expression` -> truthy if result has any records
- [x] `None`, `False`, `0`, empty list, empty dict, empty string -> falsy (no match)
- [x] Invalid JMESPath expression -> `poll_error` logged with expression detail
- [x] Condition met -> run created via `create_run()` with `trigger_type='polling'`
- [x] Condition not met -> `no_match` logged, no run created

### Run Creation
- [x] Run created with `input_payload` containing `records`, `total`, `poll_query`
- [x] `snapshot_id` resolved from `trigger.config_json.snapshot_id` (falls back to `uuid.uuid4()`)
- [x] TriggerEvent logged with `result='condition_met'` and `run_id`

### Concurrency & Limits
- [x] Active run count checked against `trigger.max_concurrent_runs` before firing
- [x] Concurrency limit reached -> `concurrency_limit_reached` logged and task returns `skipped`
- [ ] Per-pipeline daily spend limit prevents polling run creation
- [ ] Queue depth / rejection mechanism for polling (webhook has it; polling does not)

### TriggerEvent Audit
- [x] TriggerEvent logged for every outcome: `condition_met`, `no_match`, `poll_error`, `concurrency_limit_reached`
- [x] `trigger_type='polling'` set on all events
- [x] Error detail captured in `error_detail` field on poll failures

### Manual / Test Endpoint
- [x] `TriggerEngine.evaluate_condition()` static method for one-off manual or test evaluation
- [x] Returns structured dict with `status`, `records`, `total`, or `error`

## Known Gaps

- BDD feature file `backend/tests/bdd/features/pipelines/scheduling.feature` is a placeholder (7-line TODO) -- zero scenarios exercise polling trigger behaviour
- No unit tests exist for `polling.py` or the polling path in `__init__.py`
- PRD §8.5 designates `polling` as v1 (not alpha); the delivery plan may need to scope this differently
- `max_concurrent_runs` uses pipeline-level active-run counting; PRD §8.5 suggests trigger-level counting (per-trigger, not per-pipeline)
- No integration test validates end-to-end: DB scheduler -> PollingFireTask -> connector query -> condition eval -> run creation
- `_build_polling_connector()` is a standalone copy of `connector_hub._build_connector()` -- drifts if connector hub gains new types or tracing wrappers; no test validates it stays in sync
- `_fetch_due_triggers` silently skips triggers with missing/invalid `connector_instance_id` with only a log warning -- should log a TriggerEvent for operator visibility
- `snapshot_id` falls back to `uuid.uuid4()` if unset or invalid in config -- this will create runs against the latest pipeline snapshot, which may not be intended; should probably block or use a predictable sentinel
- Polling trigger has no `retain_payload` equivalent (webhook does for replay) -- intentional but undocumented
