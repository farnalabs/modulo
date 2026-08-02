---
id: feat-core-polling-trigger
prd: 8.5
delivery-tasks: [task-nv10-polling-trigger]
bdd:
  - backend/tests/bdd/features/triggers/polling.feature
  - backend/tests/bdd/features/pipelines/scheduling.feature
code:
  - backend/src/modulo/core/trigger_engine/__init__.py
  - backend/src/modulo/core/trigger_engine/polling.py
depends-on: [feat-connectors-hub]
unit-tests:
  - backend/tests/unit/trigger_engine/test_polling.py
  - backend/tests/unit/trigger_engine/test_polling_connector_drift.py
  - backend/tests/bdd/steps/test_polling_triggers.py
status: partial
---

# Polling Trigger

Discovered from 1 completed delivery tasks. Also specified in PRD 8.5 (Trigger System) as a v1 polling type using connector-based condition evaluation.

## Behaviours

### Schedule & Firing

- [x] DatabasePollingScheduler queries `triggers` table for `trigger_type='polling'` and `next_fire_at <= now()` on each beat tick
- [x] DatabasePollingEntry is created per matching trigger row
- [x] Polling triggers fire as per-item SAQ jobs (fire_due_triggers cron -> fire_polling_trigger on the runs queue)
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
- [x] Daily spend limit (`trigger.daily_spend_limit`) checked before the connector query — over-budget trigger logs `spend_limit_reached`, advances `next_fire_at`, and returns `skipped`
- [x] `daily_spend_limit` is settable/readable via the trigger CRUD API — `POST /pipelines/{id}/triggers` and `PUT /triggers/{id}` accept it on create/update (explicit `null` clears it) and every trigger response echoes it (`list_triggers`, `create_trigger`, `update_trigger`, `restore_trigger`, `list_pipeline_triggers`)
- [x] `daily_spend_limit` is settable/readable via `PATCH /triggers/{id}/polling` (explicit `null` clears it)
- [x] Negative `daily_spend_limit` is rejected with 422 by FastAPI validation (`ge=0`)
- [x] `Trigger.daily_spend_limit` defaults to `None` (unlimited) on the MCP `create_trigger` tool, which also accepts `max_concurrent_runs` and `daily_spend_limit`
- [ ] Queue depth / rejection mechanism for polling (webhook has it; polling does not)

### TriggerEvent Audit

- [x] TriggerEvent logged for every outcome: `condition_met`, `no_match`, `poll_error`, `concurrency_limit_reached`
- [x] `spend_limit_reached` TriggerEvent logged with daily limit + today's cost when the trigger is over budget
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
- [x] Daily spend limit exceeded -> `spend_limit_reached` TriggerEvent logged, run not created
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
- [x] Daily spend limit reached (`today_cost >= limit`) → no run created, `spend_limit_reached` logged, `next_fire_at` advanced so the trigger is not re-fetched every beat tick
- [x] `daily_spend_limit` unset → spend check skipped entirely, trigger fires normally
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
- BDD feature file `scheduling.feature` has 5 cron scenarios and 3 polling scenarios (all `@awaiting-implementation`) — cron scenarios in `triggers/cron.feature` and polling scenarios in `triggers/polling.feature` provide the executable BDD coverage
- PRD 8.5 designates `polling` as v1 (not alpha); delivery plan may need re-scoping
- `max_concurrent_runs` uses pipeline-level active-run counting; PRD 8.5 suggests trigger-level counting (per-trigger, not per-pipeline)
- `_build_polling_connector()` is a standalone copy of `connector_hub._build_connector()` — 35+ types excluded; drift parity test exists but doesn't prevent behavioral drift
- Polling trigger has no `retain_payload` equivalent (webhook does for replay)
- `_get_engine()` creates standalone engine outside app lifecycle — connection pool not managed by app
- `DatabasePollingScheduler._sync_with_db()` calls `asyncio.run()` per tick — new event loop every 30s
- No queue depth / rejection mechanism for polling
- Redis mid-session failure not handled (triggers stop firing, no reconnection)

## QA History

### 2026-08-02 (round 2) — Cross-cutting QA (improve-architecture)

**Findings fixed:**
- MINOR: RESOLVED the Known Gap "Trigger-level `daily_spend_limit` is enforced at fire time but not exposed via the trigger CRUD/polling-config API — set only via the DB or admin paths". `daily_spend_limit` is now accepted on `TriggerCreate`, `TriggerUpdate`, and `PollingConfigUpdate` (validated `ge=0`; explicit `null` clears it via `model_fields_set`, omitted `None` leaves it unchanged) and echoed as `daily_spend_limit` on every trigger response (`list_triggers`, `create_trigger`, `update_trigger`, `restore_trigger`, `update_polling_config`, `list_pipeline_triggers`). Added the same exposure to the MCP `create_trigger` tool (`max_concurrent_runs` + `daily_spend_limit` params, validated). Added 7 unit tests in `test_triggers_endpoint.py` (create echo + constructor arg, negative → 422, update set/clear, polling-config set/clear, list echo). Updated product map (`polling-trigger.md` + `trigger-system.md`).

### 2026-08-02 — Cross-cutting QA (improve-architecture)

**Findings fixed:**
- MAJOR: Implemented the documented Known Gap / `[ ]` behaviour "Per-pipeline daily spend limit prevents polling run creation" (also listed in `feat-core-trigger-system`: "Daily spend limit applies to cron triggers only — polling has no spend limit check"). `fire_polling_trigger` now checks `trigger.daily_spend_limit` via new `_daily_spend_limit_reached()` helper — scoped to the trigger id, org, and today's runs (`created_at >= midnight`), using `>=` comparison and a `None` no-limit short-circuit. Check runs right after the concurrency check and before the connector query so an over-budget trigger stops polling the external service. On limit reached: logs `spend_limit_reached` TriggerEvent (detail includes limit + today's cost), advances `next_fire_at` via `_update_next_fire_no_last` (prevents re-fetch every 30s beat tick), and returns `{"status": "skipped", "reason": "spend_limit", ...}`.
- MAJOR: Wired up real BDD coverage for the polling fire path — `triggers/polling.feature` was orphaned (9 scenarios, zero step definitions); rewrote it to 9 fully-executable fire-path scenarios and created `backend/tests/bdd/steps/test_polling_triggers.py` (mirrors `test_agent_signal.py` fire-path pattern). Added 3 spend-limit scenarios (limit reached → skipped, below limit → fires, no limit → fires).
- MINOR: Added 5 unit tests in `test_polling.py` (`TestDailySpendLimit`): limit reached skip + event + next_fire advance, `today_cost == limit` boundary, below-limit fires, no-limit fires, spend query SQL scoping (trigger_id/org/created_at). Updated `_make_trigger` (defaults `daily_spend_limit=None`) and `_setup_session_for_polling` (routes `total_cost_usd` spend query).
- MINOR: Updated product map (behaviours `[ ]`→`[x]`, Error Handling / TriggerEvent Audit / Edge Cases sections, Known Gaps RESOLVED, `bdd:` + `unit-tests:` frontmatter).

**Test results:** 63/63 polling unit tests pass (58 + 5 new); 9/9 polling BDD scenarios pass. Status: partial (queue-depth mechanism remains open; spend-limit API exposure resolved 2026-08-02 round 2).

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
