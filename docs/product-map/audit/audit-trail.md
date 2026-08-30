---
id: feat-audit
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/audit.py
  - backend/src/modulo/core/audit_logger/__init__.py
  - backend/src/modulo/core/audit_logger/append_only.py
  - backend/src/modulo/db/models/audit_event.py
  - frontend/src/views/AdminAuditView.vue
unit-tests:
  - backend/tests/unit/audit_logger/test_audit_logger.py
  - backend/tests/unit/audit_logger/test_append_only.py
  - backend/tests/unit/api/test_audit.py
  - backend/tests/unit/api/test_audit_bdd.py
  - backend/tests/unit/api/test_audit_gating.py
  - backend/tests/integration/test_audit_append_only.py
  - backend/tests/integration/test_audit_immutability.py
bdd:
  - backend/tests/bdd/features/audit/event_recording.feature
  - backend/tests/bdd/features/audit/audit_viewer.feature
  - backend/tests/bdd/features/admin/audit_export.feature
  - backend/tests/bdd/steps/test_audit.py
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
- [x] Events form a tamper-evident hash chain — each event's `previous_hash`
      links to the prior event in the org's chain, appends are serialized under
      a per-org `AuditChainHead` row, and verification recomputes the whole
      chain and reports the first break (`verify_chain`)
- [x] Append-only enforcement is defense-in-depth: an application-layer ORM
      guard raises `AppendOnlyViolationError` on any UPDATE/DELETE, backed by
      database-level append-only triggers (`append_only.py`; integration suites
      `test_audit_append_only` / `test_audit_immutability`)
- [x] `GET /api/v1/admin/audit` lists events with cursor pagination
      (`next_cursor` + `total`) and filters by event_type, date range, actor
      user, and resource
- [x] `GET /api/v1/admin/audit/verify` recomputes and reports per-org chain
      integrity with an event count
- [x] `GET /api/v1/admin/audit/export` streams a paginated CSV export
      (items/total/page/page_size) honouring the same filters — compliance
      evidence surface
- [x] `GET /api/v1/admin/audit/batch-detail` resolves a batch of event ids into
      full records
- [x] The audit surface is admin-only and gated by the `audit_viewer` feature
      key — 403 for non-admin, 401 unauthenticated (`test_audit_gating`,
      audit_export.feature)
- [x] Cross-domain product events are recorded: HITL output delivery, HITL
      claim expiry, org deletion requests, fernet key rotation, and run
      lifecycle (event_recording.feature scenarios)

## Known Gaps

- **Chain is per-organisation** — the hash chain, verification, and export are
  scoped to one org (multi-tenant RLS); there is no system-wide cross-org
  chain.
- **No BDD scenario for append-only tampering** — UPDATE/DELETE blocking is
  pinned by unit + integration suites only; the BDD covers recording and the
  viewer surface.

## QA History

- 2026-08-29: **improve-architecture (product-map walk)** — new behaviour
  tracker for the registered `feat-audit` manifest feature (route `/admin/audit`,
  previously absent from the feature graph). Behaviours verified against
  `api/routes/audit.py`, `core/audit_logger/*`, the hash-chain model, and the
  unit/integration/BDD suites. Status: covered.
