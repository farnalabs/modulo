---
id: feat-core-evidence-store
prd: N/A
adr: []
code:
  - backend/src/modulo/db/models/evidence.py
  - backend/src/modulo/db/migrations/versions/0263_evidence_layer.py
  - backend/src/modulo/core/eval_engine/evidence_layer.py
  - backend/src/modulo/core/eval_engine/author_warnings.py
  - backend/src/modulo/core/evidence_retention.py
  - backend/src/modulo/api/routes/admin_evidence_retention.py
  - backend/src/modulo/core/pipeline_engine/evidence.py
unit-tests:
  - backend/tests/architecture/test_evidence_deletion_carveout.py
  - backend/tests/unit/core/test_evidence.py
  - backend/tests/unit/core/test_evidence_fetch.py
  - backend/tests/unit/core/test_evidence_write_auth.py
  - backend/tests/unit/core/evidence/test_retention.py
  - backend/tests/unit/api/test_evidence_retention_routes_coverage.py
  - backend/tests/integration/db/test_evidence_retention.py
  - backend/tests/integration/test_evidence_table.py
  - backend/tests/unit/pipeline_engine/test_evidence_probes.py
depends-on: []
status: covered
---

# Core Evidence Store

The platform's append-only evidence fact store (FAR-966 chunk 7 §3.1) plus the
chunk-9a surfaces built on it: the per-org evidence retention policy and purge
sweep (FAR-961) and the advisory author warnings for racy/indeterminate evidence
keys (FAR-957). Evidence rows are key/value facts scoped to a subject
(`{subject_type, subject_id}`) inside an organisation; run nodes, guardrail
policy gates and evals reference evidence keys at pipeline runtime, so this
store is the shared substrate the `feat-evals` / `feat-guardrails` surfaces
build on. Infra-only surface — no UI route, so it is tracked here rather than in
the manifest registry.

## Behaviours

- [x] The `evidence` table is an append-only key/value fact store scoped to a
      subject (`{subject_type, subject_id}`) within an org; the `value` JSONB
      column carries boolean / number / null (indeterminate) facts and
      `producer_type` is CHECK-constrained to the five-value vocabulary
      `eval` / `policy_gate` / `run` / `system_state` / `derived`
      (`db/models/evidence.py`)
- [x] RLS org-isolation is enforced by migration `0263_evidence_layer`; the ORM
      exposes no `update()` / `delete()`, and DELETE from the table is forbidden
      except through the sanctioned retention sweep — the E1 append-only gate is
      pinned by `tests/architecture/test_evidence_deletion_carveout.py`
- [x] The evidence layer fetches subject-scoped evidence (`fetch()` returns the
      most recent row per distinct key with `DISTINCT ON` for cross-key snapshot
      consistency) and enforces producer write-authorisation via the
      `KEY_NAMESPACE_OWNERSHIP` map (`assert_write_authorisation`); the CHECK
      constraint remains authoritative for the producer-type vocabulary
      (`core/eval_engine/evidence_layer.py`)
- [x] Run-evidence fold-in maps existing `RunEvidence` tri-state rows into the
      generic evidence format (`map_run_evidence_to_evidence`) so legacy run
      evidence becomes queryable as evidence facts
- [x] The retention policy is per-org and stored in `Organisation.settings_json`
      under the `evidence_retention` key: `max_age_days` (default 90),
      `batch_size` (default 500), `lock_timeout_seconds` (default 30) and an
      optional `max_rows` cap (`core/evidence_retention.py`)
- [x] Admin REST surface, org-scoped: `GET` + `PUT /api/v1/admin/evidence-retention`
      read/update the policy (GET also returns the live `current_row_count`) and
      `POST /api/v1/admin/evidence-retention/purge` triggers a manual purge —
      all gated on `require_system_or_org_admin("evidence_retention.manage")`;
      org admins are bound to their own org (a mismatched `organisation_id` 403s),
      a system admin must name the target org (missing → 422), and the standard
      repo error mapping applies (migrations missing → 501, DB blip → 503)
- [x] The purge sweep is the single sanctioned deletion path for evidence rows
      (§2.4); it acquires a per-org advisory lock through the shared
      `PostgresLock` service (namespaced `evidence_retention:<org_id>`) and a
      second invoker that cannot acquire the lock within the timeout exits
      cleanly (idempotent)
- [x] The sweep purges oldest rows older than `max_age_days` in batched
      SAVEPOINTs, then (when `max_rows` is set) purges the oldest excess over
      the count limit; it never commits mid-sweep, so the transaction-local
      `app.organisation_id` RLS scope survives every batch — the unscoped
      batches-2..N failure a mid-sweep COMMIT caused is pinned at the unit tier
      (`tests/unit/core/evidence/test_retention.py`)
- [x] Deletion observability: every batch emits a structured log event and the
      `modulo_evidence_retention_deletions_total` OTel counter (tagged with the
      `organisation_id`), and the purge response carries `rows_deleted` /
      `batches` / `max_age_days` / `max_rows` (no-op when no meter is wired)
- [x] Advisory author warnings (FAR-957 §3): policy-gate binding
      (`POST`/`PUT /api/v1/evals/{eval_id}/policy-gate`) runs
      `check_author_warnings` for the eval's `evidence_key` and the
      `PolicyGateResponse` carries advisory (never blocking) `warnings` for three
      conditions — `no_producer` (no guaranteed producer in eval definitions /
      evidence-producing node config / system-state patterns),
      `temporal_ordering` (producer node runs downstream of the gate's binding
      node, or the binding position is indeterminate) and `recent_undefined` (the
      key produced undefined in recent runs); failure falls back to warnings (the
      safe direction) and a warning never prevents binding
      (`core/eval_engine/author_warnings.py`, wired from `api/routes/evals.py` —
      also tracked under `feat-evals`)

## Known Gaps

- **The predicate language and `decide` function are deferred.** The evidence
  layer's predicate evaluation and the compatibility adapter (§4) remain parked
  — reads today are the subject-scoped fetch + author-warning checks, not a
  general decision predicate over the store.
- **Author warnings are advisory and can be noisy on purpose.** They warn on the
  safe direction (indeterminate producer position, store-query failure), so a
  gate's rigour is ultimately the author's judgment.

## QA History

- 2026-09-30: **Improve Architecture product-map walk** — new behaviour tracker
  closing the feature-graph gap left by chunk 9a (FAR-961 / FAR-957): the
  append-only evidence store, the retention policy + purge sweep and the
  advisory author warnings shipped in `e64ddac0e` without a product-map home —
  invisible to Assistant's `search_documentation` indexer and the feature graph.
  Behaviours verified against `db/models/evidence.py`, migration 0263,
  `core/eval_engine/evidence_layer.py`, `core/eval_engine/author_warnings.py`,
  `core/evidence_retention.py`, `api/routes/admin_evidence_retention.py` and the
  retention / author-warning / deletion-carveout unit + integration suites.
  Status: covered.
