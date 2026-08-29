---
id: feat-audit
prd: N/A
adr:
  - docs/adr/004-user-deactivation-replaces-deletion.md
code:
  - backend/src/modulo/api/routes/audit.py
  - backend/src/modulo/core/audit_logger/
  - backend/src/modulo/core/audit_logger/append_only.py
  - backend/src/modulo/db/models/audit_event.py
  - frontend/src/views/AdminAuditView.vue
unit-tests:
  - backend/tests/unit/audit_logger/test_audit_logger.py
  - backend/tests/unit/audit_logger/test_append_only.py
bdd:
  - backend/tests/bdd/features/audit/audit_viewer.feature
  - backend/tests/bdd/features/audit/event_recording.feature
depends-on: []
status: covered
---

# Audit Trail

An immutable, org-scoped record of every significant action in the platform
(core principle §3 — "Audit as a first-class output"). Every event carries the
actor, action, resource and timestamp, is SHA-256 chained to its predecessor
via `previous_hash`, and is enforced append-only at both the database (Postgres
triggers) and ORM layers. Surfaces: `/admin/audit` (`AdminAuditView.vue`) with
browse/filter, chain verification, and CSV/JSONL export (`feat-audit`).

## Behaviours

- [x] `GET /api/v1/admin/audit` returns cursor-paginated audit events with
      `total` + `next_cursor`, filterable by event type (`action_type`), actor
      (`user_id`), resource type (`entity_type`) and a `from_date`/`to_date`
      window; an invalid UUID `user_id` is rejected with a 422
      (`audit_viewer.feature`, `list_audit_events`)
- [x] Events are isolated per organisation via RLS (`set_rls_org`) — an org sees
      exactly its own events; a cross-org principal observes an empty log rather
      than another tenant's records (`audit_viewer.feature` "Cross-org isolation")
- [x] `POST /api/v1/admin/audit/batch-detail` resolves a batch of event ids to
      full detail including `payload_json`, gated behind the `audit_viewer`
      feature license (`audit_viewer.feature` "Batch detail returns full events")
- [x] `GET /api/v1/admin/audit/verify` verifies the SHA-256 chain integrity for
      the org; a tampered chain reports the precise broken event and the expected
      hash instead of a blanket failure (`audit_viewer.feature`,
      `event_recording.feature` "Chain verification reports tamper evidence",
      `verify_chain`)
- [x] `GET /api/v1/admin/audit/export` streams paginated JSON (page/page_size
      bounded 1..1000) with the same filters; `AdminAuditView.vue` pages through
      it to render downloadable `audit-log-*.csv` and `.jsonl` files (local blob
      download, per the manifest elements `admin-audit-export-csv` /
      `admin-audit-export-jsonl`)
- [x] Event recording: every state-changing action appends an `AuditEvent` with
      a valid SHA-256 hash over the event payload, `previous_hash` linking to the
      prior event, the acting `account_id`, `resource_type`/`resource_id` and a
      `request_id`; verifiable chains build from any sequence of appends
      (`event_recording.feature` "Append multiple events form a verifiable chain",
      `_compute_event_hash` / `append_audit_event`)
- [x] Cross-cutting surfaces record into the trail: HITL manual delivery
      (`hitl.output_delivered` with output hash, `hitl.manual_delivery`),
      claim expiry (`hitl.claim_expired`), org deletion requests
      (`org_deletion_requested`), model-backend failover (`model_failover`),
      and schema-migration plan/apply (`schema_migration_planned` /
      `schema_migration_completed`, dry-run included)
      (`event_recording.feature`, `schema_migration.feature`)
- [x] Immutability is enforced at two layers: Postgres-level append-only
      triggers and an ORM-level guard (`audit_logger/append_only.py`) that
      raises `AppendOnlyViolationError` on any UPDATE/DELETE of an
      `AuditEvent`/`ErrorEvent` (`event_recording.feature` "Audit events are
      immutable", `test_append_only.py`)

## Known Gaps

- **Backend export is JSON-only** — CSV/JSONL files are assembled client-side in
  `AdminAuditView.vue` from the paginated `/export` JSON; there is no server-side
  CSV/JSONL rendition endpoint.
- **No BDD scenarios for the ORM append-only guard** — the ORM-layer
  `AppendOnlyViolationError` path (non-Postgres backends) is unit-verified only;
  the BDD immutable-event scenario exercises the composite surface.
- **Batch-detail and export surfaces are feature-gated, not permission-locked to
  `audit.manage`** — they depend on the `audit_viewer` license key rather than the
  same `require_permission` the top-level list endpoint uses.

## QA History

- 2026-08-29: **improve-architecture (product-map walk)** — added this
  behaviour-tracker for the registered manifest feature `feat-audit`, which had no
  `docs/product-map/` entry. Behaviours verified against
  `api/routes/audit.py`, `core/audit_logger/` (+`append_only.py`),
  `db/models/audit_event.py`, the `audit_viewer`/`event_recording` BDD features
  and the `test_audit_logger`/`test_append_only` unit suites. Status: covered.