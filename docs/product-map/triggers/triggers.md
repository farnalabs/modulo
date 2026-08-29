---
id: feat-triggers
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/triggers.py
  - backend/src/modulo/api/routes/webhooks.py
  - backend/src/modulo/api/routes/admin_triggers.py
  - backend/src/modulo/core/trigger_engine/
  - backend/src/modulo/core/trigger_streak.py
  - backend/src/modulo/core/trigger_validation.py
  - backend/src/modulo/core/cron_helpers.py
  - backend/src/modulo/db/models/trigger.py
  - backend/src/modulo/db/models/trigger_event.py
  - backend/src/modulo/db/models/webhook.py
  - frontend/src/views/SettingsTriggersView.vue
  - frontend/src/views/SettingsTriggerEventLogView.vue
unit-tests:
  - backend/tests/unit/api/test_triggers_endpoint.py
  - backend/tests/unit/api/test_admin_triggers.py
  - backend/tests/unit/api/test_trigger_config_secrets.py
  - backend/tests/unit/api/test_slack_trigger_endpoint.py
  - backend/tests/unit/core/test_trigger_streak_engine.py
  - backend/tests/unit/core/test_trigger_validation.py
  - backend/tests/unit/trigger_engine/test_trigger_engine.py
  - backend/tests/unit/test_trigger_engine_pre_guardrail.py
bdd:
  - backend/tests/bdd/features/triggers/manual.feature
  - backend/tests/bdd/features/triggers/cron.feature
  - backend/tests/bdd/features/triggers/polling.feature
  - backend/tests/bdd/features/triggers/ongoing.feature
  - backend/tests/bdd/features/triggers/webhook_hmac.feature
  - backend/tests/bdd/features/triggers/webhook_payload_mapping.feature
  - backend/tests/bdd/features/triggers/flood_protection.feature
  - backend/tests/bdd/features/triggers/agent_signal.feature
  - backend/tests/bdd/features/triggers/pause.feature
  - backend/tests/bdd/features/triggers/trigger_event_log.feature
depends-on:
  - feat-runs
status: covered
---

# Triggers

Manual, webhook, polling, cron, ongoing and agent-signal triggers for firing
pipeline runs — plus the org-wide pause kill-switch, the immutable per-delivery
`TriggerEvent` audit log, payload mapping, flood protection, and HMAC-SHA256
webhook verification. Driven by the `TriggerEngine` service and surfaced at
`/settings/triggers` (`feat-triggers`).

## Behaviours

- [x] Manual trigger endpoint (`POST /api/v1/runs`) fires a run with optional
      `run_context`, records `trigger_type: manual`, and 404s on a missing
      pipeline (`manual.feature`)
- [x] Cron triggers validate the cron expression (with next-fire preview),
      honour a timezone, fire via the `fire_due_triggers` system cron, populate
      the run input from an input template, respect the daily spend limit, and
      log a `TriggerEvent` per fire; disabled triggers do not fire
      (`cron.feature`, `core/cron_helpers.py`)
- [x] Polling triggers schedule `next_fire_at` from `poll_interval_seconds`,
      run a connector `poll_query` and evaluate a JMESPath `condition_expression`;
      a met condition fires the pipeline, `max_concurrent_runs` and the daily
      spend limit are respected, and invalid JMESPath / connector failure logs a
      `poll_error` (inactive triggers skipped) (`polling.feature`,
      `core/trigger_engine/polling.py`)
- [x] Ongoing triggers top a pipeline up to a target of in-flight runs, count
      pending runs toward the target, respect the daily spend limit, and pause
      when the org is paused (`ongoing.feature`,
      `core/trigger_validation.validate_ongoing_config`)
- [x] Webhook verification: HMAC-SHA256 signature (`sha256=...`) over
      `timestamp.body` with the encrypted secret, plus the `X-Modulo-Timestamp`
      ±300s replay-window check; missing/invalid signature or expired timestamp
      rejects with a typed, event-logged error (`webhook_hmac.feature`,
      `verify_hmac` / `verify_timestamp` in `core/trigger_engine/__init__.py`)
- [x] Payload mapping: incoming webhook fields route into the run input via
      dot-notation paths, with reserved input-payload keys (`_work_item_id`,
      `_modulo.work_item`, `_feedback_correction`) rejected as mapping targets so
      a trigger cannot forge them; `event_filters` and `accepted_events` gates
      fail closed (`webhook_payload_mapping.feature`,
      `_apply_payload_mapping`)
- [x] Flood protection: duplicate webhook payloads within the 5-minute TTL are
      deduplicated by SHA-256 hash (`DuplicateWebhookError`), and a configured
      rate-limit budget (keyed by `key_fields` with `exact`/`presence` matching)
      enforces a per-window cap (`rate_limited` event +
      `PipelineRateLimitError`); deliveries at `max_concurrent_runs` are
      accepted-and-queued rather than 429'd (`flood_protection.feature`,
      `.github/...` dedup + rate-limit paths)
- [x] A pre-trigger guardrail pass runs at the intake boundary (FAR-214) BEFORE
      the dedup insert; a blocked delivery records a `guardrail_blocked` event,
      stores the raw payload for replay, and raises
      `GuardrailBlockedAtIntakeError` — replays re-run the pass detection-only
      (`core/trigger_engine/pre_guardrail.py`,
      `unit/test_trigger_engine_pre_guardrail.py`)
- [x] Webhook replay re-fires a previously accepted delivery from its stored
      raw payload (typed `ReplayNotFoundError` on a bad/expired event)
      (`unit/trigger_engine/test_trigger_engine.py`)
- [x] Agent-signal triggers fire child runs when a watched node completes,
      respect `max_concurrent_runs`, honour org isolation, inherit the source
      input payload, ignore inactive triggers, and log a `TriggerEvent` on every
      outcome; child fires are suppressed when the source run was
      guardrail-blocked (terminal `eval_failed` + `eval_blocked`) (`agent_signal.feature`,
      `core/trigger_engine/agent_signal.py`, `is_guardrail_blocked_run`)
- [x] Org-wide pause kill-switch: an admin pause/unpause of all pipeline
      triggers, non-admin → 403, webhooks delivered to a paused org are dropped
      with a paused response, and unpause restores acceptance (`pause.feature`,
      `admin_triggers.py` + `ensure_triggers_resumable`)
- [x] Every delivery/fire writes an immutable `TriggerEvent` (accepted, rejected,
      deduplicated, rate-limited, guardrail-blocked, etc.) surfaced through a
      paginated log endpoint and the `SettingsTriggerEventLogView` (`trigger_event_log.feature`)
- [x] Trigger-config secrets (webhook HMAC secrets, connector credentials) are
      stored encrypted and masked from API responses (`unit/api/test_trigger_config_secrets.py`)
- [x] Slack app-mention trigger surface (`POST .../slack`, `core/trigger_engine/slack_app_mention.py`);
      end-to-end behaviour unit-tested (`unit/api/test_slack_trigger_endpoint.py`)

## Known Gaps

- **Slack app-mention is unit-tested only** — no dedicated BDD feature file for
  the slack trigger (BDD covers webhook/cron/polling/manual/ongoing/pause/agent-signal).
- **Webhook replay has no BDD scenario** — re-fire semantics are pinned by unit
  tests (`unit/trigger_engine/test_trigger_engine.py`) only; the BDD webhook
  flows exercise the original-delivery path.
- **Polling-trigger connector/heartbeat matrix is not BDD-covered end to end** —
  poll failures and fail-closed shared-budget behaviour are unit-verified rather
  than exercised against every provider backend.
- **Trigger-streak enforcement is unit-tested only** (`test_trigger_streak_engine.py`);
  the SAQ cron wiring that refills streaks is not scenario-locked.

## QA History

- 2026-08-27: **improve-architecture (product-map walk)** — added this entry to
  close the coverage gap for the registered `feat-triggers` feature (no
  behaviour-tracker existed). Behaviours verified against
  `core/trigger_engine/`, the `/api/v1/triggers` + webhook + admin-trigger
  routes, the 10 trigger BDD feature files, and the trigger unit suites. Status:
  covered.