---
id: feat-audit
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/audit.py
  - backend/src/modulo/core/audit_logger/__init__.py
  - backend/src/modulo/core/audit_logger/append_only.py
  - backend/src/modulo/db/crud/pipeline.py
  - backend/src/modulo/db/crud/pipeline_snapshot_versioning.py
  - backend/src/modulo/api/mcp_server.py
  - backend/src/modulo/db/models/audit_event.py
  - frontend/src/views/AdminAuditView.vue
unit-tests:
  - backend/tests/unit/audit_logger/test_audit_logger.py
  - backend/tests/unit/audit_logger/test_append_only.py
  - backend/tests/unit/api/test_audit.py
  - backend/tests/unit/api/test_audit_bdd.py
  - backend/tests/unit/api/test_audit_gating.py
  - backend/tests/unit/db/crud/test_pipeline_graph_updated_audit.py
  - backend/tests/integration/test_audit_append_only.py
  - backend/tests/integration/test_audit_immutability.py
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

## Known Gaps

- **Chain is per-organisation**: the hash chain, verification, and export are
  scoped to one org (multi-tenant RLS); there is no system-wide cross-org
  chain.

## QA History
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
