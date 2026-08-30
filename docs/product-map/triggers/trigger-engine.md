---
id: feat-triggers
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/triggers.py
  - backend/src/modulo/api/routes/webhooks.py
  - backend/src/modulo/api/routes/slack.py
  - backend/src/modulo/core/trigger_engine/__init__.py
  - backend/src/modulo/core/trigger_engine/polling.py
  - backend/src/modulo/core/trigger_engine/agent_signal.py
  - backend/src/modulo/core/trigger_engine/slack_app_mention.py
  - backend/src/modulo/core/cron_helpers.py
  - backend/src/modulo/db/models/trigger.py
  - backend/src/modulo/db/models/trigger_event.py
  - frontend/src/views/SettingsTriggersView.vue
  - frontend/src/views/SettingsTriggerEventLogView.vue
unit-tests:
  - backend/tests/unit/trigger_engine/test_trigger_engine.py
  - backend/tests/unit/trigger_engine/test_polling.py
  - backend/tests/unit/trigger_engine/test_polling_connector_drift.py
  - backend/tests/unit/trigger_engine/test_polling_shared_redis.py
  - backend/tests/unit/trigger_engine/test_agent_signal.py
  - backend/tests/unit/trigger_engine/test_slack_app_mention.py
  - backend/tests/unit/cron_scheduler/test_cron_validation.py
  - backend/tests/unit/api/test_triggers_endpoint.py
  - backend/tests/unit/api/test_admin_triggers.py
  - backend/tests/unit/api/test_webhooks_endpoint.py
  - backend/tests/unit/api/test_webhook_replay.py
  - backend/tests/unit/api/test_trigger_config_secrets.py
bdd:
  - backend/tests/bdd/features/triggers/manual.feature
  - backend/tests/bdd/features/triggers/cron.feature
  - backend/tests/bdd/features/triggers/webhook_hmac.feature
  - backend/tests/bdd/features/triggers/webhook_payload_mapping.feature
  - backend/tests/bdd/features/triggers/flood_protection.feature
  - backend/tests/bdd/features/triggers/polling.feature
  - backend/tests/bdd/features/triggers/ongoing.feature
  - backend/tests/bdd/features/triggers/agent_signal.feature
  - backend/tests/bdd/features/triggers/pause.feature
  - backend/tests/bdd/features/triggers/trigger_event_log.feature
  - backend/tests/bdd/steps/test_cron_triggers.py
  - backend/tests/bdd/steps/test_polling_triggers.py
  - backend/tests/bdd/steps/test_ongoing_triggers.py
depends-on: []
status: covered
---

# Triggers

Manual, webhook, cron, polling, ongoing, and agent-signal triggers that start
pipeline runs on demand or on a schedule, plus the org-wide pause kill-switch
and the immutable per-trigger event log. Configured via `/settings/triggers`;
webhook delivery is HMAC-authenticated, timestamp-bounded, deduplicated, and
rate-limited by the `TriggerEngine`.

## Behaviours

- [x] Trigger CRUD via `/api/v1/triggers` (list, create, PUT update, delete,
      restore) with per-type config (webhook / cron / polling / ongoing);
      secret fields are encrypted at rest and never returned
      (`test_trigger_config_secrets`, `test_triggers_endpoint`)
- [x] Manual run trigger: `POST /api/v1/runs` returns 202 and starts a pending
      run carrying caller-supplied `run_context`
- [x] Webhook delivery (`POST /api/v1/triggers/{id}/webhook`): HMAC-SHA256
      signature and `X-Modulo-Timestamp` freshness (±300s replay window) are
      required — missing/invalid HMAC or stale timestamp → 401, unknown trigger
      → 404, accepted → 202
- [x] Flood protection: a duplicate payload hash → 400 and rapid duplicates are
      rate-limited → 429
- [x] Webhook payload mapping: event filters and field mappings are applied to
      build the run context before dispatch
- [x] Cron triggers: cron expression + IANA timezone are validated (invalid
      → 422), `next_fire_at` is computed and advanced after a fire, and the
      scheduler creates runs with `trigger_type cron`
- [x] Cron preview (`GET /api/v1/triggers/{id}/cron/preview`) returns the
      upcoming scheduled fires for an expression/timezone without persisting
- [x] Polling triggers evaluate a connector condition on a schedule (JMESPath),
      record a `TriggerEvent` with result `condition_met` / `no_match`, fire a
      run when met, and respect `max_concurrent_runs`
- [x] Ongoing triggers top the pipeline up toward a target in-flight count,
      respecting `max_concurrent_runs` and the org daily spend limit
- [x] Agent-signal triggers fire a child pipeline when a watched node
      completes (`trigger_type agent_signal`), respecting the concurrency limit
- [x] Org-wide pause kill-switch (`PUT /api/v1/admin/orgs/{org}/triggers/pause`):
      webhooks delivered to a paused org are dropped with a paused response and
      create no runs
- [x] Every delivery/evaluation is recorded to an immutable `TriggerEvent` log;
      `GET /api/v1/triggers/{id}/events` is paginated
- [x] Webhook replay (`POST /api/v1/triggers/{id}/webhook/replay/{event_id}`)
      re-fires a prior event, skipping HMAC/timestamp validation while
      preserving dedup and flood protection

## Known Gaps

- **Slack app-mention triggering is unit-tested only** — no dedicated BDD
  `.feature` for `slack_app_mention.py`; unit suites
  (`test_slack_app_mention.py`, `test_slack_trigger_endpoint.py`) pin the
  behaviour.
- **Trigger config secrets use a single fernet key** — at-rest encryption
  depends on the environment `FERNET_KEY`; key rotation is handled as a domain
  operation (audited), not per-trigger.

## QA History

- 2026-08-29: **improve-architecture (product-map walk)** — new behaviour
  tracker for the registered `feat-triggers` manifest feature (route
  `/settings/triggers`, previously absent from the feature graph). Behaviours
  verified against `api/routes/triggers.py`, `api/routes/webhooks.py`, the
  `core/trigger_engine/*` package, `core/cron_helpers.py`, and the trigger
  unit/BDD suites. Status: covered.
- 2026-08-30: **duplicate-entry reconciliation** — a parallel product-map walk
  had added a second `feat-triggers` tracker at `configure/triggers.md`, breaking
  the one-entry-per-feature invariant. This entry is the superset and is
  retained; the duplicate's unique citations (`api/routes/slack.py`, the polling
  connector-drift and shared-redis unit suites) were folded in here. Status:
  covered.
