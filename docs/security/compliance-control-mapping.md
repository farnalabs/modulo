# Compliance control mapping

This document describes Modulo's compliance control-mapping pack and the
per-instance evidence index generated from it.

**Audience:** platform engineers, security and compliance owners, and the
external auditor examining a self-hosted Modulo instance.

## What this is, and what it is not

Modulo holds no SOC 2, ISO/IEC 42001 or EU AI Act attestation. That is a
deliberate decision, not a gap to be papered over.

In a self-hosted deployment the customer's auditor audits **the customer's own
instance**, not a Farnalabs service. What that auditor needs is not our
certificate; it is an account of the evidence the instance produces and an
honest map from that evidence to the clauses being tested. This pack and the
evidence index are that account.

What they do **not** do:

- They do not attest that the customer's system is compliant. Only the
  customer's auditor can reach that conclusion.
- They do not test the customer's controls. They record what Modulo observed.
- They do not cover activity outside Modulo's execution boundary. Rows for
  that are marked `customer-responsibility` or `gap`, with the responsibility
  stated.

## Where the pack lives

The mapping content ships in the product as versioned YAML:

```
backend/src/modulo/compliance/mappings/
  pack.yaml          pack version, publication date, framework index
  soc2.yaml          SOC 2 Trust Services Criteria
  iso-42001.yaml     ISO/IEC 42001
  eu-ai-act.yaml     EU AI Act
```

The loader (`backend/src/modulo/compliance/pack.py`) validates the pack at
load time and refuses an invalid or dishonest one:

- every row names a known artefact type, coverage attribution and provenance
  tag;
- every row states, in a sentence, what remains the customer's responsibility;
- a `satisfied` row can never rest on customer-supplied material;
- a `customer-responsibility` row must be tagged `customer-supplied`;
- artefact types the instance does not produce cannot evidence a
  `satisfied`/`supported` claim;
- every framework must name at least one thing Modulo does not satisfy
  (a pack that claims everything is worse than no pack);
- the required SOC 2 clause set is present;
- every indexed artefact type is mapped to at least one clause.

## Vocabulary

### Coverage (per row, per framework)

| Value | Meaning |
|---|---|
| `satisfied` | Modulo enforces the control and produces the evidence for it, within the stated boundary. |
| `supported` | Modulo supplies evidence or a mechanism that contributes to the clause; the customer completes it. |
| `customer-responsibility` | Modulo does not satisfy the clause. The evidence is the customer's own record. |
| `gap` | Neither Modulo nor the shipped documentation provides the capability. The responsibility is stated. |

There is deliberately **no single verdict** anywhere in the pack or the
response. Coverage is reported per row and per framework.

### Provenance (per row)

| Value | Meaning |
|---|---|
| `observed` | Modulo recorded the fact directly (for example an audit event or a run snapshot). |
| `derived` | The evidence is computed from Modulo-observed records. |
| `customer-supplied` | The evidence must come from the customer. It is never presented as Modulo's. |

## The shipped pack at a glance

| Framework | Rows | satisfied | supported | customer-responsibility | gap |
|---|---:|---:|---:|---:|---:|
| SOC 2 TSC | 16 | 3 | 9 | 4 | 0 |
| ISO/IEC 42001 | 10 | 1 | 7 | 2 | 0 |
| EU AI Act | 9 | 1 | 4 | 4 | 0 |

These counts are pinned by `backend/tests/unit/compliance/test_mapping_pack.py`.
They change only with an explicit pack-version bump.

A `gap` count of zero is a deliberate, evidenced outcome - not a missing row.
The vocabulary defines `gap` as "neither Modulo nor the shipped documentation
provides the capability",
and the shipped `docs/security/incident-response-playbook.md` does provide
incident-report and post-mortem templates - so the EU AI Act Article 73 row is
honestly `customer-responsibility` (detecting and reporting a serious incident
remains entirely the customer's obligation) rather than `gap`. Every framework
still names several things Modulo does not itself satisfy, which is the property
that matters.

## The per-instance evidence index

### Endpoint

```
GET /api/v1/compliance/evidence-index
```

- Authenticated, admin-only (`compliance.evidence_index.read`). The index
  aggregates the organisation's whole audit surface, so it sits at the same
  level as `audit.manage`, not at the viewer-level `audit.list`.
- The organisation comes from the authenticated principal. There is no
  organisation parameter and no cross-tenant read.
- No UI ships with this ticket; the endpoint is the deliverable an auditor is
  handed. A UI can follow in a later ticket.

### What it contains

One entry per artefact type **the instance produces**, always present in the
response:

| Artefact type | Source in the instance | Provenance |
|---|---|---|
| `audit_chain` | `audit_events` + `audit_chain_heads`; browse and verify endpoints (the signed export is separately gated) | observed |
| `run_snapshot` | `pipeline_snapshots` joined to `runs` | observed |
| `hitl_decision` | decided `hitl_claims`, counting `human_only` gates separately | observed |
| `eval_result` | `eval_results`, with pass/fail counts and definition count | observed |
| `autonomy_change` | `pipeline.autonomy_promotion_decided` / `pipeline.autonomy_demoted` audit events | observed |
| `release_sbom` | release pipeline artefact (or `backend/scripts/generate-sbom.py`), retained by the operator | customer-supplied |
| `build_identity` | `GET /api/v1/deployment` (product version, source revision, build timestamp) | observed |

The index does **not** list every artefact the pack maps, because it cannot: a
row whose `artifact_type` is `customer_records` is evidenced by material Modulo
never sees. Ten mapping rows across the three frameworks rest on
`customer_records` - policies, risk assessments, internal-audit reports,
vulnerability-management records, incident records and restore-test records.
Those are the row classes the customer must evidence themselves; each row's
`responsibility` field says what is owed, and each row's `customer_location`
says where to look. The customer-facing rows are:

- **SOC 2** CC6.6 (perimeter/host hardening), CC6.7 (encryption in transit and
  at rest), CC7.4 (incident response), CC7.5 (backup and restore).
- **ISO/IEC 42001** 8.2 (AI risk assessment), 9.2 (internal audit).
- **EU AI Act** Article 9 (risk-management system), Article 17
  (quality-management system), Article 72 (post-market monitoring), Article 73
  (serious-incident reporting).

Each entry carries:

- `artifact_type`, `status` (`present` / `absent` / `customer_supplied`);
- `provenance`, `description`, `location` (where the customer finds it);
- `identifiers` (a bounded sample: chain head hash, snapshot ids, decision
  ids, result ids, event ids, version and revision);
- `record_count`, and a small `detail` object with the aggregate counts. Counts
  come from aggregate queries and are exact, with one exception: the
  `hitl_decision` entry's `human_only_gate_decisions` /
  `human_only_gate_approvals` are derived from a bounded scan of 10,000 decided
  rows, so when it truncates they are **lower bounds** and the entry sets
  `human_only_counts_are_lower_bounds: true` (plus
  `human_only_scan_truncated: true`). An auditor reading those figures must
  read the flag with them;
- `mapping_row_ids` - the pack rows this artefact evidences;
- `absence_reason` when the status is `absent`.

`hitl_decision`'s `human_only_gate_approvals` counts the decision vocabulary's
two approval outcomes, `approved` **and** `deliver_manual` (a reviewer who
supplies the output directly has approved the gate). `rejected` is not an
approval. Whether a gate counts as human-only is resolved through the product's
single rule (`human_only_effective`): an omitted, `null` or non-dict
`human_only` key means **true**, so legacy and raw-JSON gates are counted as
human oversight rather than skipped.

A `present` `audit_chain` entry means the trail exists **in this instance**. It
does not assert that the offline-verifiable evidence-pack export is available
here: that export additionally needs the Team-tier `audit_viewer` feature and a
configured `MODULO_EVIDENCE_SIGNING_KEY`, and the index does not probe either.

### Missing artefacts are loud, never dropped

A mapped artefact type with no records for the organisation is reported with
`status: absent`, an `absence_reason`, and its name in
`missing_artifact_types`. The response also carries
`complete: false`. An auditor therefore sees the hole in the evidence, not an
index that silently looks complete.

The generator additionally supports `strict=True`, which raises
`EvidenceIndexIncompleteError` naming every missing type instead of returning a
partial index. No automated export ships with this release: `strict` is a
library-level option a caller may use, not a gate any shipped endpoint applies.

Note what counts as absent: an organisation that has run no pipeline has no run
snapshots, no HITL decisions and no eval results. That is not an error in the
index - it is the truth about that instance's evidence, and the index says so.

### Audit-period pinning

The response records:

- `pack_version` - the pack release that produced the index;
- `pack_content_hash` - a SHA-256 over every asserted row, so an edit that did
  not bump the version is still detectable;
- `generated_at`, `product_version` and `organisation_id`.

Retain the index with the evidence pack for the audit period. A later release
cannot silently change what was asserted for a past period: compare the
recorded version and hash against the pack shipped with the release that was
deployed at the time.

## Using it in an audit

1. Deploy the release under audit and record `GET /api/v1/deployment`.
2. Prove the audit chain in place with `GET /api/v1/admin/audit/verify`. The
   endpoint accepts no query parameters and is not period-scoped: it checks the
   first **10,000** events in created-at order and returns `truncated: true`
   with `valid: false` when the organisation's trail is larger. That is an
   incomplete check, not evidence of tampering. Where the trail exceeds that
   cap, verify through the chain export instead (step 3), which carries the
   events under a signature the auditor checks offline - do not read
   `valid: false` alone as an integrity failure.
3. If the export is required, license the Team tier (`audit_viewer`) and
   configure `MODULO_EVIDENCE_SIGNING_KEY`, then take the evidence pack
   (`GET /api/v1/admin/audit/evidence-pack`) and the verification key
   (`GET /api/v1/admin/audit/verification-key`). Without both, both routes
   answer 402/503; the in-instance chain remains browsable and verifiable.
4. Generate the evidence index (`GET /api/v1/compliance/evidence-index`) and
   retain it with the pack. Treat any entry with `status: absent` as a finding
   to explain, not a blank to fill in.
5. For each `customer-responsibility` row (see the list above), attach the
   customer's own record - policy, risk assessment, restore test, incident
   report, SBOM. The pack names the responsibility, so the split between
   platform evidence and customer evidence is explicit.
6. Hand the auditor the pack version and content hash, the evidence index, and
   - where the export preconditions are met - the offline-verifiable evidence
   pack with its public key.

## Honest limitations

- The audit chain proves what Modulo recorded and that the record is
  internally consistent. It does not prove that the recorded facts are true.
- The chain verifier checks at most 10,000 events per call, always the first
  10,000 in created-at order, and reports `valid: false` when it truncates. It
  takes no date range, so there is no date-bounded verification to fall back on.
  A `false` therefore means "not proven complete", not "tampered".
- The offline-verifiable export is not on by default. It needs the Team-tier
  `audit_viewer` feature (`GET /api/v1/admin/audit/evidence-pack` and
  `/verification-key` answer 402 without it) and a configured
  `MODULO_EVIDENCE_SIGNING_KEY` (503 without it). On a Community install the
  trail is browsable and verifiable in place but cannot be exported in a form
  that survives leaving the instance.
- Run-retention configuration is the Team-tier `admin_run_retention` feature.
  Evidence retention is admin-only and not plan-gated. Do not describe the two
  as one setting.
- The SBOM is produced at release time and retained by the operator. The
  running instance does not hold it, so the index reports it as
  `customer_supplied` rather than pretending to observe it.
- Backup, restore testing, perimeter security, identity-provider configuration
  and incident response are the customer's in a self-hosted deployment, and
  the pack marks them as such.
- Clause identifiers follow the published standards and regulations. The
  customer should confirm the exact wording and applicability against their
  licensed copies and their auditor's scope.
- There is no third-party attestation report attached to this pack. If a
  questionnaire asks for one, the answer is that Modulo does not hold one; see
  `docs/security/security-questionnaire-answer-bank.md` for how to answer such
  questions honestly.
