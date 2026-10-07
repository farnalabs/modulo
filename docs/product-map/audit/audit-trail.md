---
id: feat-audit
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/audit.py
  - backend/src/modulo/api/routes/admin_system_audit.py
  - backend/src/modulo/core/audit_logger/__init__.py
  - backend/src/modulo/core/audit_logger/append_only.py
  - backend/src/modulo/core/audit_logger/background.py
  - backend/src/modulo/core/audit_coverage.py
  - backend/src/modulo/core/cron_helpers.py
  - backend/src/modulo/core/run_admission.py
  - backend/src/modulo/core/run_terminal_advance.py
  - backend/src/modulo/core/runner_capacity.py
  - backend/src/modulo/core/saq_worker.py
  - backend/src/modulo/db/crud/pipeline.py
  - backend/src/modulo/db/crud/system_audit_event.py
  - backend/src/modulo/db/crud/pipeline_snapshot_versioning.py
  - backend/src/modulo/db/seed.py
  - backend/src/modulo/api/mcp_server.py
  - backend/src/modulo/db/models/audit_event.py
  - backend/src/modulo/core/system_audit_logger.py
  - backend/src/modulo/db/models/system_audit_event.py
  - backend/src/modulo/db/migrations/versions/0285_system_audit_events.py
  - frontend/src/views/AdminAuditView.vue
unit-tests:
  - backend/tests/unit/audit_logger/test_audit_logger.py
  - backend/tests/unit/audit_logger/test_append_only.py
  - backend/tests/unit/audit_logger/test_background_audit.py
  - backend/tests/architecture/test_background_audit_coverage.py
  - backend/tests/unit/api/test_audit.py
  - backend/tests/unit/api/test_audit_bdd.py
  - backend/tests/unit/api/test_audit_gating.py
  - backend/tests/unit/api/test_audit_coverage.py
  - backend/tests/unit/api/test_far1464_route_arm_coverage.py
  - backend/tests/unit/api/test_webhooks_endpoint.py
  - backend/tests/unit/api/test_admin_system_audit.py
  - backend/tests/unit/core/test_background_audit_wiring.py
  - backend/tests/unit/core/eval_engine/test_execute_suite_run.py
  - backend/tests/unit/db/crud/test_pipeline_graph_updated_audit.py
  - backend/tests/unit/db/test_seed_users.py
  - backend/tests/unit/crud/test_system_audit_event.py
  - backend/tests/unit/core/test_system_audit_logger.py
  - backend/tests/integration/test_audit_append_only.py
  - backend/tests/integration/test_audit_immutability.py
  - backend/tests/integration/test_system_audit_org_deletion.py
bdd:
  - backend/tests/bdd/features/audit/event_recording.feature
  - backend/tests/bdd/features/audit/append_only.feature
  - backend/tests/bdd/features/audit/audit_viewer.feature
  - backend/tests/bdd/features/admin/audit_export.feature
  - backend/tests/bdd/steps/test_audit.py
  - backend/tests/bdd/steps/test_audit_append_only.py
  - backend/tests/bdd/features/admin/test_audit_export_steps.py
depends-on: []
status: covered
---

# Audit Trail & Audit Log

Immutable, hash-chained audit trail of significant actions across the
organisation, plus the admin log surface (`/admin/audit`) that lists, filters,
verifies, and exports events for security review and SOC 2-style compliance
evidence. Every substantial product action (HITL decisions, org deletion,
secret/key rotation, run lifecycle) appends an `AuditEvent`, and the chain is
guarded against tampering at both the ORM and the database layer.

## Behaviours

- [x] Significant actions append an `AuditEvent` carrying event_type, actor,
      resource type/id, organisation, JSON payload, and request_id, with a
      SHA-256 hash of the canonical payload
- [x] Events form a tamper-evident hash chain – each event's `previous_hash`
      links to the prior event in the org's chain, appends are serialized under
      a per-org `AuditChainHead` row, and verification recomputes the whole
      chain and reports the first break (`verify_chain`)
- [x] Append-only enforcement is defense-in-depth: an application-layer ORM
      guard raises `AppendOnlyViolationError` on any UPDATE/DELETE, backed by
      database-level append-only triggers (`append_only.py`; executing BDD
      `append_only.feature` drives the real guard's UPDATE/DELETE/INSERT paths,
      and integration suites `test_audit_append_only` / `test_audit_immutability`
      pin the storage layer)
- [x] `GET /api/v1/admin/audit` lists events with cursor pagination
      (`next_cursor` + `total`) and filters by event_type, date range, actor
      user, and resource
- [x] `GET /api/v1/admin/audit/verify` recomputes and reports per-org chain
      integrity with an event count
- [x] `GET /api/v1/admin/audit/export` streams a paginated CSV export
      (items/total/page/page_size) honouring the same filters – compliance
      evidence surface
- [x] `GET /api/v1/admin/audit/scan` streams the WHOLE matching event set in
      ONE response (NDJSON by default, or a Content-Disposition CSV attachment)
      – the server-side scan companion to `/export` with the same typed filters
      and `audit_viewer` + `audit.manage` gates, but no offset/limit pagination:
      the server keyset-paginates over the stable `(created_at, id)` order in
      fixed page batches so memory stays bounded for any org size
      (`core/audit_logger/__init__.py` `stream_export_chain`,
      `api/routes/audit.py` `scan_chain_endpoint`,
      `tests/unit/api/test_audit_scan_route.py`,
      `TestScanChain` in `tests/unit/api/test_audit.py`)
- [x] `GET /api/v1/admin/audit/batch-detail` resolves a batch of event ids into
      full records
- [x] The audit surface is admin-only and gated by the `audit_viewer` feature
      key – 403 for non-admin, 401 unauthenticated (`test_audit_gating`,
      audit_export.feature)
- [x] Cross-domain product events are recorded: HITL output delivery, HITL
      claim expiry, org deletion requests, fernet key rotation, and run
      lifecycle (event_recording.feature scenarios)
- [x] Pipeline graph mutations are ALWAYS audited (FAR-1471): every successful
      `replace_pipeline_graph` write AND every `rollback_to_snapshot` appends
      one `pipeline.graph_updated` event in the SAME transaction as the write
      (so a graph write can never commit without its audit event), carrying a
      concise before/after summary – node/edge counts plus the added / removed
      / `agent_commands`-changed node IDS only, never node payloads,
      `env_vars`, `context_files` or parameter values, so no masked secret can
      enter the chain. MCP graph writes attribute the event to the caller's
      account id (`changed_by`; a session without one honestly records null,
      never a bogus id), and the previously HITL-only audit left plain writes
      such as an `agent_commands` edit unattributable
      (`db/crud/pipeline.py` `graph_update_audit_payload` + `GRAPH_UPDATED_EVENT`,
      `db/crud/pipeline_snapshot_versioning.py`, `api/mcp_server.py`,
      `test_pipeline_graph_updated_audit.py`)
- [x] Pre-auth and webhook routes are audited through the actor-less
      `audited_system` variant (FAR-1516): sign-in/out, token refresh, the
      SAML ACS POST, the public error ingest and inbound webhooks record the
      same isolated-append event WITHOUT fabricating an actor — `actor_user_id`
      stays NULL, the payload carries the `SYSTEM_ACTOR` marker plus an
      `actor_source` string (`pre_auth` / `unauthenticated` /
      `signature_verified` / `authenticated`) stating HOW the request was
      admitted, never WHO; the route publishes its tenant via
      `bind_audit_org(request, org_id)` at the point it becomes known (with no
      tenant yet, the unattributed `SYSTEM_ORG_ID` sentinel is used so the
      attempt is still recorded, and the real org rebinds the moment it
      resolves), an event with no published org is logged
      (`audit_coverage.<event_type>.no_org_context`) and skipped rather than
      written into a fabricated tenant, and `bind_audit_actor_source()` lets
      the route STRENGTHEN its declared `actor_source` once a verified
      signature or an authenticated principal lands — recorded provenance is
      always the strongest TRUE admission statement
      (`core/audit_coverage.py` `audited_system` / `bind_audit_org` /
      `bind_audit_actor_source`, applied in `api/routes/auth.py`, `sso.py`,
      `slack.py`, `stripe_webhook.py`, `webhooks.py`, `errors.py`;
      `test_audit_coverage.py`, `test_webhooks_endpoint.py`,
      `test_far1464_route_arm_coverage.py`)
- [x] Org-lifecycle audit records survive a hard delete via an org-independent
      durable ledger (FAR-1517). `audit_events.organisation_id` FKs
      `organisations.id` with `ON DELETE CASCADE`, so a hard-deleted org took
      its ENTIRE chain with it – including the `org_deletion_requested` row
      written moments earlier – and a post-commit append could never satisfy
      the FK on an org that no longer exists. The five org-lifecycle writers
      (`DELETE /api/v1/admin/org`, `POST /api/v1/admin/org/deletion-request`,
      `POST /api/v1/admin/org/deletion-confirm`,
      `PATCH /api/v1/admin/org/deletion-cancel`, and the system-admin
      `DELETE /api/v1/admin/orgs/{org_id}`) now mirror the same evidence
      (`org_deletion_requested` / `org_deletion_completed` /
      `org_deletion_cancelled`) into `system_audit_events` **inside the
      deleting transaction, before the org row is removed**: the record commits
      only if the delete commits, and a failed append aborts the destructive
      act (fail-closed – the writer deliberately has no try/except; route
      handlers map the raised `SQLAlchemyError` family to 5xx). The table
      deliberately has **no `organisation_id` tenant column and no FK**: the
      org id is a plain `org_id` value, so no cascade reaches it and no RLS
      scope excludes it – the record stays readable by an operator long after
      the org is gone. UPDATE/DELETE are rejected by database append-only
      triggers, the same structural guard `audit_events` carries (the ledger is
      deliberately **not** hash-chained: the tamper-evident chain is
      per-organisation and its head cascades away, so immutability here comes
      from the triggers). The three routes are recorded in
      `tests/architecture/audit_coverage_baseline.txt` because `audited()`'s
      post-commit org-scoped append cannot record anything after a hard delete
      (`core/system_audit_logger.py`, `db/models/system_audit_event.py`,
      migration 0285, the writer files `api/routes/admin.py` and
      `api/routes/admin_orgs.py`,
      `tests/integration/test_system_audit_org_deletion.py`,
      `tests/unit/core/test_system_audit_logger.py`)
- [x] Background, cron and boot write paths are audited or explicitly
      classified (FAR-1549). Writes that never see a request (SAQ system-cron
      sweeps, runs-worker tasks, reconcilers, boot seeds) either append a
      SYSTEM-actor event through the shared `append_background_audit_event` /
      `record_run_state_change_audits` / `record_suite_run_audit` helpers —
      `actor_user_id` stays NULL with
      a `SYSTEM_ACTOR` payload marker plus an `actor_source` naming the process,
      the org RLS context is set inside the helper's own fresh transaction, the
      batch helper re-selects each run and drops any whose live status is not an
      expected one (the phantom-event guard), and an append failure is logged and
      swallowed after the already-committed mutation (fail open) with
      `CancelledError` always propagating — or carry a documented exemption from
      a fixed reason vocabulary
      (`EXEMPT_REASONS`: trigger bookkeeping, notification-only, ephemeral
      log retention, derived cache/state/analytics, probe bookkeeping,
      liveness, telemetry watermark, internal bookkeeping, boot config,
      demo fixture, infra-container GC). `tests/architecture/test_background_audit_coverage.py`
      mechanically enumerates every path from the SAQ registration functions,
      the `CronJob` list, the `_boot_seed(...)` labels and the `core/`
      reconciler/sweep/seed functions, so a new background path fails the gate
      until classified. FAR-1561 closed the two gaps FAR-1549 left visible: the
      SuiteRun lifecycle appends `suite_run_created` (fire, before enqueue) and
      `suite_run_started` / `suite_run_completed` (execution, post-commit) with
      the same re-select phantom guard, and the `modulo_users` boot seed appends
      `user_seeded` / `user_rehashed` inside the seeding transaction — the
      credential and any admin-role grant commit atomically with their record —
      so no enumerated path is classified `gap` any more
      (`core/audit_logger/background.py`,
      `core/cron_helpers.py`, `core/run_admission.py`,
      `core/run_terminal_advance.py`, `core/runner_capacity.py`,
      `core/saq_worker.py`, `db/seed.py`, `test_background_audit.py`,
      `test_background_audit_wiring.py`, `test_background_audit_coverage.py`,
      `test_seed_users.py`, `test_execute_suite_run.py`)
- [x] The durable org-lifecycle ledger has a system-admin read surface
      (FAR-1538): `GET /api/v1/admin/system-audit` lists the org-independent
      `system_audit_events` records read-only with offset pagination and
      event-type / org-id / date-range filters, gated purely by
      `require_system_permission` (the `is_system_admin` claim — no RLS org
      context, because the table has no `organisation_id` column, and no
      `audited()` dependency, because reads are not audited), and the
      `/admin/audit` page exposes a system source tab with the same filters,
      pagination and a detail panel that reveals the full identifiers
      (`api/routes/admin_system_audit.py`, `db/crud/system_audit_event.py`,
      `frontend/src/views/AdminAuditView.vue`, `test_admin_system_audit.py`,
      `test_system_audit_event.py`)

## Known Gaps

- **Chain is per-organisation**: the hash chain, verification, and export are
  scoped to one org (multi-tenant RLS); there is no system-wide cross-org
  chain. The org-independent `system_audit_events` ledger (FAR-1517) survives a
  hard delete and now has a system-admin read surface (FAR-1538 —
  `GET /api/v1/admin/system-audit` plus the `/admin/audit` system tab), but it
  is deliberately **not** hash-chained and has no chain-verify endpoint — it is
  durable, filterable evidence storage, not a verifiable cross-org trail.

## QA History
- 2026-10-07: **Improve Architecture product-map walk** — closed two untracked
  audit sub-surfaces merged after the 2026-10-06 walk. (1) FAR-1549 audited the
  background/cron/boot write paths with a new shared
  `core/audit_logger/background.py` helper plus an architecture ratchet
  (`test_background_audit_coverage.py`) that mechanically enumerates every SAQ
  task, cron job, reconciler, seeder and boot seed and fails until each is
  classified audited / exempt / gap; added the checked behaviour line and the
  code + unit-test citations. (2) FAR-1538 added the system-admin read surface
  for the durable `system_audit_events` ledger (`GET /api/v1/admin/system-audit`
  + the `/admin/audit` system tab), narrowing the "no read API/UI" half of the
  per-organisation-chain Known Gap; added the checked behaviour line and
  citations. `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-10-06: **Improve Architecture product-map walk** — closed the untracked
  FAR-1516 surface: pre-auth and webhook routes (sign-in/out, token refresh,
  SAML ACS, public error ingest, inbound webhooks) ship an actor-less
  `audited_system` audit variant that never fabricates an actor — recording a
  `SYSTEM_ACTOR` marker + `actor_source` admission basis with the tenant
  published by the route (`bind_audit_org`, unattributed `SYSTEM_ORG_ID`
  sentinel fallback) and promotable via `bind_audit_actor_source` — but did not
  appear in either product-map layer. Added the checked behaviour line and the
  `code:` / `unit-tests:` citations (`core/audit_coverage.py`;
  `test_audit_coverage.py`, `test_webhooks_endpoint.py`,
  `test_far1464_route_arm_coverage.py`) plus the `frontend/src/manifest.yaml`
  `feat-audit` registry line. `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-10-06: **Improve Architecture product-map walk**: closed the untracked
  FAR-1517 surface. Org-lifecycle events (`org_deletion_requested`,
  `org_deletion_completed`, `org_deletion_cancelled`) previously lived only in
  the org-scoped `audit_events` chain, which cascades away with a hard-deleted
  organisation; they are now mirrored IN the deleting transaction into the
  org-independent, append-only `system_audit_events` ledger
  (`core/system_audit_logger.py`, `db/models/system_audit_event.py`, migration
  0285). Added the checked behaviour line, the code and integration/unit test
  citations, and clarified the existing "per-organisation chain" Known Gap (the
  ledger is durable but not chained and has no read surface). `_ORPHANED_BDD_FEATURES`
  stays empty.
- 2026-10-05: **Improve Architecture product-map walk**: closed the untracked
  FAR-1471 surface: every pipeline graph mutation (the `replace_pipeline_graph`
  write path and `rollback_to_snapshot`) now appends a `pipeline.graph_updated`
  audit event in the same transaction, with an IDs-and-counts-only before/after
  payload, and MCP graph writes stamp the caller's account id as `changed_by`.
  Added the checked behaviour line plus the `db/crud/pipeline.py`,
  `db/crud/pipeline_snapshot_versioning.py`, `api/mcp_server.py` code citations
  and the `test_pipeline_graph_updated_audit.py` unit citation.
  `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-10-03: **Improve Architecture product-map walk**: closed the
  feat-audit deferral "the export surface is paginated JSON only (no server-side
  streaming / scan export for the whole org in one response)." New
  `GET /api/v1/admin/audit/scan` streams the whole org event set as NDJSON/CSV
  (the server keyset-paginates over `created_at ASC, id ASC` in fixed batches),
  mirroring the feat-analytics `/scan` surface; unit + TestClient coverage pins
  the core generator, the wire shape, error mapping and the 401/403/422 gates.
  Removed the deferral from the manifest registry and the tracker.
- 2026-10-02: **Improve Architecture product-map walk**: closed
  the `personas/marcus-ciso.feature` "Marcus verifies the audit log is
  append-only" journey gap (pinned `@awaiting-implementation` since 2026-08
  while the feature shipped underneath it). The scenario now executes against
  the REAL `register_append_only_guard` + `before_update`/`before_delete`
  listeners via `steps/test_personas.py` (real `AuditEvent` row in an in-memory
  engine; UPDATE and DELETE attempts both rejected with
  `AppendOnlyViolationError`; original event intact, timestamped, attributable
  afterwards) and was removed from
  `PINNED_AWAITING_IMPLEMENTATION` (`test_test_suite_safety_nets.py`).
- 2026-09-17: **product-map review pass**: closed "No BDD
  scenario for append-only tampering". New executing `audit/append_only.feature`
  (`steps/test_audit_append_only.py`) drives the REAL application-layer guard:
  `register_append_only_guard()` + the SQLAlchemy `before_update` /
  `before_delete` listeners are exercised against persisted `AuditEvent` /
  `ErrorEvent` rows in an in-memory engine – UPDATE and DELETE are rejected on
  both models with an `AppendOnlyViolationError` that names the event id and
  the mutation, while a plain INSERT is not blocked. The placeholder
  "Audit events are immutable" scenario (which merely asserted a generic 4xx
  from a nonexistent PATCH route) was removed along with its dummy steps.
  `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-09-12: **product-map review pass**: registered the shared
  `JsonViewer` surface (`components/shared/JsonViewer.vue` static testids
  `json-viewer` / `json-viewer-{copy,expand-all,collapse-all,string-expand,string-collapse}`)
  in the manifest `elements:` inventory for `/admin/audit`: the expanded audit-event
  payload is rendered inline with `<JsonViewer :show-toolbar="true">` (`AdminAuditView.vue`),
  so the viewer shipped in the DOM while staying invisible to Assistant's docs indexer /
  `/api/v1/manifest`. The component is now part of the route's reverse testid-coverage
  guard (`test_mapped_route_elements_cover_owning_view_testids`).

- 2026-09-12: **product-map review pass**: registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/admin/audit`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Assistant's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-11: **product-map review pass**: extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/admin/audit`: the whole-page view(s) `AdminAuditView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Assistant's docs indexer /
  `/api/v1/manifest`.

- 2026-08-29: **product-map review pass**: new behaviour
  tracker for the registered `feat-audit` manifest feature (route `/admin/audit`,
  previously absent from the feature graph). Behaviours verified against
  `api/routes/audit.py`, `core/audit_logger/*`, the hash-chain model, and the
  unit/integration/BDD suites. Status: covered.
