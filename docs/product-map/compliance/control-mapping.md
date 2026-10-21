---
id: feat-compliance
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/compliance.py
  - backend/src/modulo/compliance/__init__.py
  - backend/src/modulo/compliance/artifacts.py
  - backend/src/modulo/compliance/pack.py
  - backend/src/modulo/compliance/evidence_index.py
  - backend/src/modulo/compliance/mappings/pack.yaml
  - backend/src/modulo/compliance/mappings/soc2.yaml
  - backend/src/modulo/compliance/mappings/iso-42001.yaml
  - backend/src/modulo/compliance/mappings/eu-ai-act.yaml
  - backend/src/modulo/auth/permissions.py
unit-tests:
  - backend/tests/unit/compliance/test_mapping_pack.py
  - backend/tests/unit/compliance/test_evidence_index.py
  - backend/tests/unit/api/test_compliance_endpoint.py
  - backend/tests/integration/test_compliance_evidence_index.py
depends-on:
  - feat-audit
  - feat-hitl
  - feat-runs
  - feat-evals
status: covered
---

# Compliance Control-Mapping Pack and Evidence Index

Modulo holds no SOC 2, ISO/IEC 42001 or EU AI Act attestation, and in a
self-hosted deployment the customer's auditor audits the customer's own
instance. FAR-2036 ships the two artefacts that make that possible: a versioned
control-mapping pack (frameworks → clauses → Modulo control → the artefact type
that evidences it → where the customer finds it → an honest coverage
attribution and provenance tag) and a per-instance evidence index that lists
what the instance actually records for one organisation, keyed to the mapping
rows, and names what is missing instead of omitting it.

Coverage is per row and per framework. The surface deliberately carries no
single compliant / not-compliant verdict.

## Behaviours

- [x] The mapping pack is shipped in-product as YAML
      (`modulo/compliance/mappings/`) and versioned independently of the
      product: `pack_version` (MAJOR.MINOR.PATCH) plus a deterministic
      `pack_content_hash` over the asserted content, both recorded in every
      generated index so a later release cannot silently change what was
      asserted for a past audit period
      (`core/compliance/pack.py::MappingPack.content_hash`,
      `tests/unit/compliance/test_mapping_pack.py`)
- [x] The pack loads fail-closed on dishonesty, not just on syntax: every row
      names a vocabulary artefact type / coverage / provenance, a `satisfied`
      row can never rest on customer-supplied material, a
      `customer-responsibility` row must be tagged `customer-supplied`, a
      non-indexed artefact cannot evidence a satisfied/supported claim, every
      framework states its `source` and `scope_note` (null/omitted rejected),
      every framework names at least one gap or customer responsibility, the
      SOC 2 minimum clause set is present, and every indexed artefact type is
      mapped to at least one clause
      (`core/compliance/pack.py::validate_mapping_rows` /
      `validate_frameworks`, `test_mapping_pack.py`)
- [x] `GET /api/v1/compliance/evidence-index` returns the caller's
      organisation's index (never a query-supplied org) behind the admin-only
      `compliance.evidence_index.read` permission; the route runs inside
      `session.begin()` with the RLS org + user context set, and every collector
      query also filters `organisation_id` explicitly, so the tenant boundary
      does not depend on either layer alone
      (`api/routes/compliance.py`, `tests/unit/api/test_compliance_endpoint.py`,
      `tests/integration/test_compliance_evidence_index.py`)
- [x] Every artefact type in `INDEXED_ARTIFACT_TYPES` appears exactly once and
      in vocabulary order, keyed to the mapping rows it evidences; a collector
      added/removed/renamed without updating the vocabulary raises rather than
      serving an index with a silent hole
      (`core/compliance/evidence_index.py::build_evidence_index`,
      `test_evidence_index.py`)
- [x] The audit-chain entry reports the org's chain head hash / chain event
      count / real event count with first and last event timestamps; events with
      no chain head are reported `absent` with a reason naming the integrity
      problem (not "no events"), and a NULL `last_event_id` does not emit a
      `None` identifier (`evidence_index.py::_collect_audit_chain`)
- [x] Run snapshots are counted as distinct frozen snapshots referenced by the
      org's runs, with both the run and the snapshot side filtered to the org
      (`_collect_run_snapshots`)
- [x] HITL decisions are counted by outcome and the human-only subset is
      resolved through the SAME precedence chain every pending surface reads -
      the claim's fire-time `gate_config_json` stamp, then the run's frozen
      snapshot config, then the fail-safe default (`DEFAULT_HUMAN_ONLY = True`).
      `deliver_manual` counts as an approval; a NULL stamp whose snapshot opted
      out of human-only is NOT counted as human oversight
      (`core/compliance/evidence_index.py::_collect_hitl_decisions`,
      `db/crud/hitl_review_config.py::resolve_review_human_only_map`,
      `test_evidence_index.py::TestHumanOnlyResolutionUsesTheFullPrecedenceChain`)
- [x] The human-only breakdown is derived from a bounded 10 000-row scan and
      says so: when the scan truncates the entry sets
      `human_only_scan_truncated` / `human_only_counts_are_lower_bounds` instead
      of presenting the figures as exact, and the scan orders NULL decision
      times last (Postgres sorts NULLs first under DESC)
- [x] Eval results report total / passed / failed / distinct eval definitions /
      latest evaluated-at for the org (`_collect_eval_results`); autonomy
      changes report counts by the two autonomy audit event types
      (`_collect_autonomy_changes`)
- [x] The release SBOM is always `customer_supplied` (the running instance does
      not hold it; the operator retains the release artefact) and build identity
      reports the product version plus `GIT_SHA` / `BUILD_TIMESTAMP` when
      present, is `absent` under the development version, and names any missing
      build fields rather than inventing them
      (`_collect_release_sbom`, `_collect_build_identity`)
- [x] A mapped artefact type with no records is reported `absent` with a reason
      and named in `missing_artifact_types` (`complete: false`) - never dropped;
      `strict=True` turns the same condition into
      `EvidenceIndexIncompleteError`. `release_sbom` is the only
      `customer_supplied` entry, so an org with no records sees no other
      non-absent org-scoped entry
      (`test_evidence_index.py::TestMissingArtefactsAreLoud`)
- [x] The index is point-in-time: `generated_at`, `product_version`, the pack
      version/hash/published-at, the framework ids and per-framework
      `coverage` counts (all four attribution values present) travel with it
      (`EvidenceIndex`, `pack.coverage_counts`)
- [x] The customer-facing mapping documentation and the security questionnaire
      answer bank ship alongside the pack and state only what the repository
      evidences (`docs/security/compliance-control-mapping.md`,
      `docs/security/security-questionnaire-answer-bank.md`)

## Known Gaps

- **API-only: there is no UI route.** The evidence index is reachable at
  `GET /api/v1/compliance/evidence-index` but no `/admin/compliance` page in the
  manifest surfaces it, so a self-hoster must call the API (or an auditor script
  built on it) to obtain the index. This is why the feature is tracked here
  rather than in the manifest `features:` registry (no route references it).
- **No BDD feature file.** Coverage is unit (`test_mapping_pack.py`,
  `test_evidence_index.py`, `test_compliance_endpoint.py`) plus one integration
  suite (`tests/integration/test_compliance_evidence_index.py`, real Postgres
  RLS). A `compliance/evidence_index.feature` driving the route end to end is
  not registered in the executing BDD suite.
- **The index is generated on demand and not retained.** There is no stored
  historical index per audit period: each call re-reads live rows and stamps the
  current pack version/hash. A customer who needs a frozen artefact for a past
  period must export it themselves while that period's rows are still retained;
  evidence retention/cleanup can remove the underlying rows.
- **The human-only breakdown is bounded.** Above 10 000 decided gates the
  human-only counts are lower bounds (flagged in the payload), because the count
  is computed in Python from a bounded scan so it behaves identically on
  Postgres and SQLite.
- **Customer artefacts are not indexed.** Rows resting on `customer_records`
  (policies, risk assessments, internal-audit reports, incident and restore-test
  records) are the customer's own evidence; the index produces no entry for that
  type and the mapping documentation lists them for the customer instead.
- **Pack-row `notes` are excluded from `content_hash`** (commentary, not an
  asserted field), so an edit confined to a row's `notes` does not change the
  hash. Every asserted field does.

## QA History

- 2026-10-21: **Improve Architecture product-map walk** - registered the
  previously untracked FAR-2036 compliance surface in the feature graph (this
  entry), and fixed one MAJOR Lens-9 List C finding (*implemented but
  unreachable in tested states*) while doing it: `_collect_hitl_decisions`
  resolved `human_only` from the claim's `gate_config_json` stamp only. That
  column is nullable by design (migration 0195; a gate fired before it existed,
  or whose fire-time stamp failed, carries NULL), and the product's pending
  surfaces resolve the same question through
  `resolve_review_human_only_map` - stamp, then the run's frozen snapshot, then
  the fail-safe default. The index therefore made `human_only_effective(None)`
  answer for a gate whose snapshot said `human_only: false`, over-reporting
  human oversight in the audit artefact the pack exists to make truthful. The
  collector now loads the decided `HitlClaim` rows and resolves through the
  shared chain, so an unstamped legacy gate is classified from its snapshot;
  the stamp still wins when present. Proved by three regression tests
  (snapshot opt-out not counted, snapshot opt-in counted, stamp-over-snapshot
  precedence) which fail against the previous one-layer read. The shipped
  auditor documentation (`docs/security/compliance-control-mapping.md`) named
  only `human_only_effective` as the rule; it now describes the full chain.
