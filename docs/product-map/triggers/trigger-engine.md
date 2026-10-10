---
id: feat-triggers
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/triggers.py
  - backend/src/modulo/api/mcp_server.py
  - backend/src/modulo/api/routes/webhooks.py
  - backend/src/modulo/api/routes/slack.py
  - backend/src/modulo/core/trigger_engine/__init__.py
  - backend/src/modulo/core/trigger_engine/polling.py
  - backend/src/modulo/core/trigger_engine/agent_signal.py
  - backend/src/modulo/core/trigger_engine/slack_app_mention.py
  - backend/src/modulo/core/trigger_streak.py
  - backend/src/modulo/core/cron_helpers.py
  - backend/src/modulo/api/team_scope.py
  - backend/src/modulo/db/crud/team_scope.py
  - backend/src/modulo/db/crud/row_lock.py
  - backend/src/modulo/db/models/trigger.py
  - backend/src/modulo/db/models/trigger_event.py
  - frontend/src/views/SettingsTriggersView.vue
  - frontend/src/views/SettingsTriggerEventLogView.vue
unit-tests:
  - backend/tests/unit/trigger_engine/test_trigger_engine.py
  - backend/tests/unit/trigger_engine/test_polling.py
  - backend/tests/unit/trigger_engine/test_polling_acl.py
  - backend/tests/unit/trigger_engine/test_polling_connector_drift.py
  - backend/tests/unit/trigger_engine/test_polling_shared_redis.py
  - backend/tests/unit/trigger_engine/test_agent_signal.py
  - backend/tests/unit/trigger_engine/test_slack_app_mention.py
  - backend/tests/unit/core/test_trigger_streak_engine.py
  - backend/tests/unit/cron_scheduler/test_cron_validation.py
  - backend/tests/unit/api/test_triggers_endpoint.py
  - backend/tests/unit/api/test_admin_triggers.py
  - backend/tests/unit/api/test_webhooks_endpoint.py
  - backend/tests/unit/api/test_webhook_replay.py
  - backend/tests/unit/api/test_trigger_config_secrets.py
  - backend/tests/unit/api/test_team_scope_dependencies.py
  - backend/tests/unit/api/test_triggers_routes_coverage.py
  - backend/tests/unit/mcp/test_trigger_crud_tools.py
  - backend/tests/unit/mcp/test_trigger_mgmt_tools.py
  - backend/tests/integration/test_trigger_run_team_gate.py
  - frontend/src/__tests__/SettingsTriggersView.spec.ts
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
  - backend/tests/bdd/features/triggers/slack_app_mention.feature
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
      required – missing/invalid HMAC or stale timestamp → 401, unknown trigger
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
- [x] Streak/outcomes readout surfaces delivery health per trigger (`streak_status`:
      enabled / streak / threshold / state / deactivated_reason / last_outcomes)
      derived from the run-outcome classification records (≤5 outcomes, newest
      first) (`core/trigger_streak.py`); each outcome's agent-reported confidence
      rides the wire (FAR-1373, vocabulary renamed from `self_reported` by
      FAR-1388): `last_outcomes` entries carry
      `delivery_confidence` (`agent_reported` on post-FAR-1336 records,
      including the pre-rename `self_reported` alias which stored rows still
      carry and which is never backfilled – readers treat either spelling as
      agent-reported; `None` –
      never "verified" – on pre-FAR-1336 six-key rows), and
      `SettingsTriggersView` renders an "Agent-reported" confidence qualifier chip
      ONLY on a `delivered` outcome whose confidence is `agent_reported` or that
      deprecated alias – a
      no_delivery / excluded / unclassified row, a non-agent-reported confidence,
      or an unknown/absent key renders no qualifier
      (`test_trigger_streak_engine.py`, `SettingsTriggersView.spec.ts`)
- [x] The no-delivery streak engine covers cron triggers as well as ongoing
      (FAR-1387): the sweep walks cron triggers' terminal classifications with
      the SAME streak walk and GREATEST(last_delivery_at, streak_epoch)
      boundary, `streak_status` returns real values for a covered cron (never
      the unconfigured base), and a cron trip NOTIFIES and SURFACES
      (append-only `cron_trigger.no_delivery_streak_alert` record + sweep
      "tripped" count) WITHOUT auto-deactivating — deactivation is opt-in per
      cron via `config_json no_delivery_auto_deactivate` (default OFF), cron
      applies its own 48h minimum wall-clock window
      (MODULO_CRON_STREAK_MIN_WINDOW_HOURS override), and cron audit records
      use `cron_trigger.*` event types, never the `ongoing_trigger.*` stream
      (`core/trigger_streak.py`, `test_trigger_streak_engine.py`)
- [x] The streak badge in `SettingsTriggersView` renders for every trigger type
      the engine covers — ongoing AND cron — via ONE mirrored constant
      (`STREAK_COVERED_TRIGGER_TYPES`, pointing at the backend
      `STREAK_TRIGGER_TYPES` in `core/trigger_streak.py`), and the "Deactivated"
      badge + re-enable action are driven by `streak_status.state ===
      'deactivated'` (`deactivated_reason`), NEVER by the trigger type: a
      notify-only cron trip (state `ok`, `deactivated_reason` null) shows only
      "No-delivery streak x/N" with `role=status`/aria-live, while "Deactivated"
      wording appears only when the backend reports the deactivated state
      (FAR-1405; `SettingsTriggersView.vue`,
      `frontend/src/__tests__/SettingsTriggersView.spec.ts`)
- [x] Trigger mutation against a team-private pipeline enforces the team gate
      (FAR-1513): access derives from the trigger's owning pipeline (triggers
      carry no team columns of their own — same derivation as runs, ADR 038) via
      `resolve_trigger_team_scope`, which INNER JOINs pipelines so RLS parity is
      free (a non-member of a team-private pipeline sees the trigger as absent →
      404, never a boundary/enumeration signal). The single `evaluate_team_gate`
      matrix (shared by the REST `require_team_membership_or_admin` dependency
      and the MCP per-row trigger guards) evaluates absent row → 404, org admin
      → allowed, org/team-private row → allowed, a team-scoped API-key bound to
      the wrong team → 403 boundary, and a user principal with no membership row
      in the owning team → 403 membership (an unset identity denies — it cannot
      prove membership, matching the RLS unknown-user posture). The check is
      re-run INSIDE the mutation transaction with the row FOR UPDATE (no TOCTOU)
      and a bounded `set_mutation_row_lock_timeout` row-lock wait. A soft-deleted
      PIPELINE denies; a soft-deleted TRIGGER still resolves so
      `POST /triggers/{id}/restore` reaches the gate — a team-private trigger
      must not become restorable by deletion (`api/team_scope.py`
      `evaluate_team_gate` / `resolve_trigger_team_scope`,
      `db/crud/team_scope.py`, `db/crud/row_lock.py`;
      `unit-tests: test_team_scope_dependencies.py,
      test_triggers_routes_coverage.py`,
      `integration: test_trigger_run_team_gate.py`,
      `bdd: features/teams/team_pipeline_visibility.feature`)
- [x] A polling trigger's connector reference is team-scoped at BOTH save and
      read (FAR-1595): a trigger's `config_json.connector_instance_id` was never
      team-validated — the fire job reads the connector row team-blind
      (`cron_helpers` sets the execution context) with only the allowlist gate,
      so a team-A trigger could name and poll a team-B connector and read its
      team-private credentials. SAVE-time: the shared
      `_validate_connector_instance_team_scope` gate is called from the REST
      `create_trigger` / `update_trigger` / `update_polling_config` paths AND the
      MCP `create_trigger` / `update_trigger` tools, judging the reference with
      `find_connector_team_mismatches`'s team-blind org-scoped read (FAR-1515
      CRITICAL 1) against the trigger's owning-pipeline `owner_team_id` (the
      value the FAR-1513 team gate already resolved, so no second
      caller-facing read), returning the named 409 `connector_team_mismatch`
      detail; only the TEAM-PRIVATE direction is refused (an org-visible
      connector is usable by any team's pipeline), an unresolvable reference
      fails CLOSED as `ConnectorBindingMissingError`, a non-UUID value is a loud
      no-op warning, and the check is a NEW-binding check (an unchanged
      `connector_instance_id` is not re-judged). READ-time defence-in-depth:
      `trigger_engine.polling.enforce_polling_team_scope` runs BEFORE any
      credential is decrypted (alongside `enforce_polling_read_acl`) and
      re-validates the connector's team scope against the owning trigger's
      pipeline through the SAME shared `connector_team_mismatch` predicate,
      DENYING fail-closed when there is no pipeline context to verify (every
      production caller forwards it: the SAQ cron fire job and
      `TriggerEngine.evaluate_condition`)
      (`backend/src/modulo/api/routes/triggers.py`,
      `backend/src/modulo/api/mcp_server.py`,
      `backend/src/modulo/core/trigger_engine/polling.py`,
      `backend/src/modulo/core/trigger_engine/__init__.py`,
      `backend/src/modulo/core/cron_helpers.py`;
      `unit-tests: test_polling_acl.py, test_triggers_routes_coverage.py,
      test_trigger_crud_tools.py, test_trigger_mgmt_tools.py`)

## Known Gaps

- **Trigger config secrets use a single fernet key** – at-rest encryption
  depends on the environment `FERNET_KEY`; key rotation is handled as a domain
  operation (audited), not per-trigger.

## QA History
- 2026-10-09: **Improve Architecture product-map walk** – closed the untracked
  FAR-1595 sub-surface (polling connector team-scope gate, merged in PR #1451):
  the middle of the connector team-scope rule — a polling trigger naming a
  team-private connector from another team — shipped with NO coverage in either
  product-map layer. The manifest `feat-triggers` registry and this tracker
  carried the FAR-1513 team-gate-but-not-connector half but not the FAR-1595
  save-time (`_validate_connector_instance_team_scope` over REST create/update/
  polling-config and the MCP trigger tools, 409 `connector_team_mismatch`,
  NEW-binding-only) or read-time (`enforce_polling_team_scope`, fail-closed on a
  missing pipeline context) halves. Added the checked behaviour line plus the
  `polling.py` / `mcp_server.py` / `cron_helpers.py` code and
  `test_polling_acl.py` / `test_triggers_routes_coverage.py` /
  `test_trigger_crud_tools.py` / `test_trigger_mgmt_tools.py` unit-test
  citations.
- 2026-10-08: **Improve Architecture product-map walk** – closed the untracked
  FAR-1513 sub-surface (team-gate trigger create/update/delete/toggle/restore
  against a team-private pipeline, merged in PR #1376): the trigger team-gate
  shipped with NO coverage in either product-map layer — the manifest
  `feat-triggers` registry and this tracker both predated it. Added the checked
  behaviour line (owning-pipeline derivation, the single `evaluate_team_gate`
  matrix, the in-transaction FOR UPDATE re-check, the restore-resolution rule)
  plus the `api/team_scope.py` / `db/crud/team_scope.py` / `db/crud/row_lock.py`
  code citations and the unit/integration/BDD citations.
  `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-10-03: **Improve Architecture product-map walk** – closed the
  `feat-triggers` tracker lag left by FAR-1387 (cron no-delivery streak
  engine, merged 2026-09-26) and FAR-1405 (no-delivery streak badge,
  merged 2026-09-27): the manifest `feat-triggers` registry carried the
  shipped cron-streak behaviour and the badge rendering rule, but the
  human-readable graph entry lagged behind (no behaviour line for either,
  and the `SettingsTriggersView.spec.ts` unit citation was absent from the
  frontmatter). Added the two checked behaviours — the notify-only-by-default
  cron streak trip (opt-in `no_delivery_auto_deactivate`, the 48h minimum
  window, `cron_trigger.*` audit event types) and the
  `STREAK_COVERED_TRIGGER_TYPES` badge rule where "Deactivated" wording is
  driven by backend state, never trigger type — plus the
  `core/trigger_streak.py` code citation and the
  `SettingsTriggersView.spec.ts` unit citation.
  `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-10-01: **Improve Architecture product-map walk** – closed the
  sub-surface gap left by FAR-1373 (merged as the streak-outcome confidence
  qualifier): the streak/outcomes readout's per-outcome `delivery_confidence`
  wire surface and the `SettingsTriggersView` "Agent-reported" qualifier chip
  (vocabulary renamed from `self_reported` by FAR-1388)
  shipped with no product-map home – the manifest registry only mentioned the
  streak UI and the tracker not at all. Added the checked behaviour line plus
  the `core/trigger_streak.py` code and `test_trigger_streak_engine.py` /
  `SettingsTriggersView.spec.ts` unit-test citations. The run-outcome
  classification record the streak derives from is tracked under `feat-runs`.
  `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-09-27: **Improve Architecture product-map walk** – closed the
  reverse-coverage guard gap for the org-wide trigger event log page
  (`/admin/trigger-events`, FAR-1255): the route's whole-page view
  `SettingsTriggerEventLogView.vue` was the last manifest route not mapped in
  `OWNED_PAGES` of `test_product_map_consistency.py`, so its six documented
  elements (`settings-trigger-event-log-*`) were invisible to the
  manifest→frontend and reverse testid drift guards. The route now maps to its
  owning view, making the reverse-coverage guard complete across every manifest
  route (a newly shipped testid on the page can no longer silently stay
  invisible to Assistant's docs indexer / `/api/v1/manifest`).
- 2026-09-23: **product-map review pass** – absorbed the last two
  `@awaiting-implementation` trigger drafts that lived under the pipelines
  directory. `pipelines/webhook_trigger.feature` (deleted) duplicated this
  entry's executing `triggers/webhook_hmac.feature` / `triggers/flood_protection.feature`
  surfaces, and `pipelines/scheduling.feature`'s cron-fire + polling scenarios
  duplicated `triggers/cron.feature` / `triggers/polling.feature` (the file now
  ships only its cron-CRUD scenarios). The webhook expired-timestamp rejection
  (`TimestampExpiredError` → 400, ±300s replay window) remains unit-pinned by
  `test_trigger_engine.py`. All trigger-delivery behaviour stays cited from this
  entry; `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-09-17: **product-map review pass** – closed the
  "Slack app-mention triggering is unit-tested only" gap: registered
  ``triggers/slack_app_mention.feature`` into the executing BDD suite from the
  new ``steps/test_slack_app_mention_triggers.py``, driving the real
  ``slack_app_mention.py`` seams – signed-request verification
  (``X-Slack-Signature`` HMAC-SHA256 + ±300s ``X-Slack-Request-Timestamp``
  replay window, wrong-secret and expired-timestamp refusals), the
  ``url_verification`` challenge echo, envelope parsing / payload mapping,
  Slack ``event_id`` deduplication, concurrency-queuing, pipeline rate
  limiting, and the advisory-lock busy refusal – each delivery audited to a
  TriggerEvent result (``accepted`` / ``hmac_failed`` / ``deduplicated`` /
  ``event_type_not_accepted`` / ``parse_failed`` /
  ``concurrency_limit_reached`` / ``rate_limited``).
  ``_ORPHANED_BDD_FEATURES`` stays empty.
- 2026-09-12: **product-map review pass** – registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/settings/triggers`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Assistant's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-11: **product-map review pass** – extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/settings/triggers`: the whole-page view(s) `SettingsTriggersView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Assistant's docs indexer /
  `/api/v1/manifest`.

- 2026-08-29: **product-map review pass** – new behaviour
  tracker for the registered `feat-triggers` manifest feature (route
  `/settings/triggers`, previously absent from the feature graph). Behaviours
  verified against `api/routes/triggers.py`, `api/routes/webhooks.py`, the
  `core/trigger_engine/*` package, `core/cron_helpers.py`, and the trigger
  unit/BDD suites. Status: covered.
- 2026-08-30: **duplicate-entry reconciliation** – a parallel product-map walk
  had added a second `feat-triggers` tracker at `configure/triggers.md`, breaking
  the one-entry-per-feature invariant. This entry is the superset and is
  retained; the duplicate's unique citations (`api/routes/slack.py`, the polling
  connector-drift and shared-redis unit suites) were folded in here. Status:
  covered.
