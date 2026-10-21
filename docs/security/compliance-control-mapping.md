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
| SOC 2 TSC | 16 | 5 | 7 | 4 | 0 |
| ISO/IEC 42001 | 10 | 2 | 6 | 2 | 0 |
| EU AI Act | 9 | 1 | 4 | 3 | 1 |

These counts are pinned by `backend/tests/unit/compliance/test_mapping_pack.py`.
They change only with an explicit pack-version bump.

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

Every artefact type in the pack's vocabulary, always present in the response:

| Artefact type | Source in the instance | Provenance |
|---|---|---|
| `audit_chain` | `audit_events` + `audit_chain_heads`; browse, verify and signed export endpoints | observed |
| `run_snapshot` | `pipeline_snapshots` joined to `runs` | observed |
| `hitl_decision` | decided `hitl_claims`, counting `human_only` gates separately | observed |
| `eval_result` | `eval_results`, with pass/fail counts and definition count | observed |
| `autonomy_change` | `pipeline.autonomy_promotion_decided` / `pipeline.autonomy_demoted` audit events | observed |
| `release_sbom` | release pipeline artefact (or `backend/scripts/generate-sbom.py`), retained by the operator | customer-supplied |
| `build_identity` | `GET /api/v1/deployment` (product version, source revision, build timestamp) | observed |

Each entry carries:

- `artifact_type`, `status` (`present` / `absent` / `customer_supplied`);
- `provenance`, `description`, `location` (where the customer finds it);
- `identifiers` (a bounded sample: chain head hash, snapshot ids, decision
  ids, result ids, event ids, version and revision);
- `record_count` and a small `detail` object with the exact counts;
- `mapping_row_ids` - the pack rows this artefact evidences;
- `absence_reason` when the status is `absent`.

### Missing artefacts are loud, never dropped

A mapped artefact type with no records for the organisation is reported with
`status: absent`, an `absence_reason`, and its name in
`missing_artifact_types`. The response also carries
`complete: false`. An auditor therefore sees the hole in the evidence, not an
index that silently looks complete.

The generator additionally supports `strict=True`, which raises
`EvidenceIndexIncompleteError` naming every missing type, so an automated
export can refuse to publish an incomplete index.

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
2. Export the evidence pack (`GET /api/v1/admin/audit/evidence-pack`) and the
   verification key (`GET /api/v1/admin/audit/verification-key`).
3. Generate the evidence index (`GET /api/v1/compliance/evidence-index`) and
   retain it with the pack. Treat any entry with `status: absent` as a finding
   to explain, not a blank to fill in.
4. For each `customer-responsibility` and `gap` row, attach the customer's own
   record (policy, risk assessment, restore test, incident report, SBOM). The
   pack names the responsibility, so the split between platform evidence and
   customer evidence is explicit.
5. Hand the auditor three things: the pack version and content hash, the
   offline-verifiable evidence pack with its public key, and the evidence
   index.

## Honest limitations

- The audit chain proves what Modulo recorded and that the record is
  internally consistent. It does not prove that the recorded facts are true.
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
