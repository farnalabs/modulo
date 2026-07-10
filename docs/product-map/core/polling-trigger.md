---
id: feat-core-polling-trigger
prd: 8.5
delivery-tasks: [task-nv10-polling-trigger]
bdd:
  - backend/tests/bdd/features/pipelines/scheduling.feature
code:
  - backend/src/modulo/core/trigger_engine/__init__.py
  - backend/src/modulo/core/trigger_engine/polling.py
depends-on: [feat-connectors-hub]
unit-tests:
  - backend/tests/unit/trigger_engine/test_polling.py
  - backend/tests/unit/trigger_engine/test_polling_connector_drift.py
status: partial
---

# Polling Trigger

Discovered from 1 completed delivery tasks. Also specified in PRD 8.5 (Trigger System) as a v1 polling type using connector-based condition evaluation.

## Behaviours

### Schedule & Firing

- [x] DatabasePollingScheduler queries `triggers` table for `trigger_type='polling'` and `next_fire_at <= now()` on each beat tick
- [x] DatabasePollingEntry is created per matching trigger row
- [x] PollingFireTask fires asynchronously via Celery with `autoretry_for=(ConnectionError, TimeoutError, OSError)`, `max_retries=2`, `default_retry_delay=30`
- [x] Trigger row re-read with `FOR UPDATE` lock to serialise concurrent fire attempts
- [x] `schedule_polling_trigger()` in TriggerEngine computes `next_fire_at` from `poll_interval_seconds` and persists it
- [x] `next_fire_at` and `last_fired_at` updated after each fire cycle (both on condition_met and no_match)
- [x] Next-fire guard: if `next_fire_at > now()`, task returns `already_fired_this_cycle` without firing
- [x] Stale trigger entries removed from in-memory schedule when DB rows are deleted/deactivated
- [x] Active/inactive toggle respected: inactive triggers are skipped without logging
- [x] RLS org isolation: all DB queries enforce `organisation_id` scoping

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
- [x] `snapshot_id` resolved from `trigger.config_json.snapshot_id` (falls back to `uuid.UUID(int=0)` with warning logged)
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

## Error Handling

- [x] `_log.warning()` logged when connector instance not found (poll_error)
- [x] `_log.warning()` logged when connector init fails (poll_error)
- [x] `_log.warning()` logged when poll query execution fails (poll_error)
- [x] `_log.warning()` logged when JMESPath condition evaluation fails (poll_error)
- [x] `_log.warning()` logged when `_fetch_due_triggers` finds a trigger with missing `connector_instance_id`
- [x] `_log.warning()` logged when `snapshot_id` config is missing or invalid (falls back to `uuid.UUID(int=0)`)
- [x] Error detail strings truncated to 200 characters (prevents internal details leaking)
- [x] Broad `except Exception` in `_fetch_due_triggers` caught and logged (returns empty list gracefully)
- [x] TriggerEvent rows written for all outcomes: condition_met, no_match, poll_error, concurrency_limit_reached
- [x] `next_fire_at` advanced on all error paths (connector_not_found, connector_init_failed, query_timeout, query_failed, condition_eval_failed) — prevents perpetual re-fetch on every beat tick

## Edge Cases

- [x] Inactive trigger → skipped with `trigger_inactive_or_missing`, no new session
- [x] `next_fire_at` in future → skipped with `already_fired_this_cycle`
- [x] Connector instance missing from DB → poll_error logged, error returned
- [x] Connector credentials fail to decrypt → poll_error logged (broad Exception catch in init block)
- [x] Connector type unsupported in polling → poll_error logged (ValueError from `_build_polling_connector`)
- [x] Poll query raises any exception → poll_error logged (broad Exception catch)
- [x] JMESPath expression invalid → poll_error logged (catch in `evaluate_condition`)
- [x] `evaluate_condition` returns `None`, `False`, `0`, `[]`, `{}`, `""` → falsy (no match)
- [x] Concurrency limit reached → no run created, concurrency_limit_reached logged
- [x] `snapshot_id` missing or invalid → falls back to `uuid.UUID(int=0)` with warning
- [ ] Redis becomes unreachable mid-session — polling triggers stop firing (no reconnection)
- [ ] Redis becomes available after starting without it — requires restart
- [ ] `redis_url` empty-string vs unset edge case

## Resilience & Integration Robustness

- [x] `_fetch_due_triggers` wraps all DB queries in try/except — returns `[]` on failure (degrade)
- [x] `PollingFireTask` has `autoretry_for=(ConnectionError, TimeoutError, OSError)` with 2 retries and 30s delay
- [x] Each polling trigger fire uses its own DB session — failure isolation
- [x] FOR UPDATE lock serialises concurrent fire attempts for same trigger
- [x] Connector errors logged as TriggerEvents — no silent data loss
- [x] Broad `except Exception` around connector init, query exec, condition eval — isolated per-service failure
- [ ] `_get_engine()` creates standalone engine outside app lifecycle — connection pool not managed by app
- [ ] `DatabasePollingScheduler` uses `asyncio.run()` per tick — new event loop every 30s

## Known Gaps

### Resolved in this iteration
- ~~`snapshot_id` falls back to `uuid.uuid4()` if unset/invalid~~ — RESOLVED: now falls back to `uuid.UUID(int=0)` with `_log.warning()` (2026-07-05)
- ~~No `_log.warning()` calls on poll_error paths~~ — RESOLVED: added `_log.warning()` to all 4 poll_error paths and config validation path (2026-07-05)
- ~~`raw_payload_hash` in TriggerEvent was static `sha256(b"polling")`~~ — RESOLVED: now includes `trigger.id` and `result` in hash (2026-07-05)
- ~~Error strings in poll responses were untruncated~~ — RESOLVED: truncated to 200 characters (2026-07-05)

### Remaining
- BDD feature file `scheduling.feature` has 5 cron scenarios and 3 polling scenarios (all `@awaiting-implementation`) — step definitions not yet wired
- PRD 8.5 designates `polling` as v1 (not alpha); delivery plan may need re-scoping
- `max_concurrent_runs` uses pipeline-level active-run counting; PRD 8.5 suggests trigger-level counting (per-trigger, not per-pipeline)
- `_build_polling_connector()` is a standalone copy of `connector_hub._build_connector()` — 35+ types excluded; drift parity test exists but doesn't prevent behavioral drift
- Polling trigger has no `retain_payload` equivalent (webhook does for replay)
- `_get_engine()` creates standalone engine outside app lifecycle — connection pool not managed by app
- `DatabasePollingScheduler._sync_with_db()` calls `asyncio.run()` per tick — new event loop every 30s
- No per-pipeline daily spend limit prevents polling run creation
- No queue depth / rejection mechanism for polling
- Redis mid-session failure not handled (triggers stop firing, no reconnection)

## QA History

### 2026-07-05 — Cross-cutting QA (improve-architecture index 156)

**Findings fixed:**
- MAJOR: Added `_log.warning()` calls to all 4 poll_error paths in `fire_polling_trigger` (connector_not_found, connector_init_failed, query_failed, condition_eval_failed)
- MAJOR: Changed `snapshot_id` fallback from `uuid.uuid4()` (random UUID) to `uuid.UUID(int=0)` with `_log.warning()` — prevents runs against non-existent snapshots
- MAJOR: Replaced static `sha256(b"polling")` payload hash with trigger-id + result based hash — `raw_payload_hash` field is now meaningful
- MINOR: Truncated error detail strings to 200 characters in poll error paths
- MINOR: Added 3 `@awaiting-implementation` BDD scenarios to `scheduling.feature` for polling trigger happy path, no-match, and error paths

**Test results:** 45/45 polling unit tests pass (42 existing + 3 new logging/hash tests).
**Status:** partial (10 known gaps remain unchanged — see Known Gaps Remaining).

### 2026-07-09 — Cross-cutting QA (improve-architecture index 289)

**Findings fixed:**
- CRITICAL: `TestPollingFireTask.test_task_attributes` asserted `PollingFireTask.autoretry_for == (Exception,)` but the code defines `autoretry_for = (ConnectionError, TimeoutError, OSError)`. The narrowed exception set is correct (avoids retrying on programming errors), but the test assertion contradicted the source code and would fail at runtime. Updated test and product map to match the actual code.
- MAJOR: All 5 error paths in `fire_polling_trigger` (connector_not_found, connector_init_failed, query_timeout, query_failed, condition_eval_failed) did not advance `next_fire_at` on the trigger row. This caused broken triggers to be re-fetched and re-failed on every beat tick (every ~30s) forever. Added `_update_next_fire_no_last(session, trigger)` call before each error return.

**Test results:** 48/48 polling unit tests pass (45 existing + 3 updated assertion).
