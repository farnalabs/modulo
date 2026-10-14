# Offline-verifiable evidence

This document defines the product term **offline-verifiable evidence** and
describes the mechanism that produces and checks it.

## Definition

**Offline-verifiable evidence** is an exported compliance artefact that a party
holding **only**:

1. the artefact, and
2. a pre-published public verification key,

can verify - with no Modulo service, no Farnalabs-operated endpoint, no network
and no login. Three things are checked, each by re-deriving it rather than
trusting it:

- **Authorship** - the artefact's canonical manifest carries a valid Ed25519
  signature under the published key.
- **Integrity of every file** - each file's SHA-256 and size are recomputed and
  compared against the signed manifest's file list.
- **The embedded audit chain** - the chain is replayed from the embedded audit
  events and the re-derived result is compared against the signed chain summary.

If any of the three disagrees, the pack is invalid. A tampered file, a mutated
event, a falsified chain claim, or a swapped verification key all fail.

## Honest boundary - what it does, and does not, prove

It proves the **integrity, authorship and reproducibility of what Modulo
recorded**: the events Modulo captured, the files it exported, and the identity
that exported them. It does **not** prove that the recorded facts are true, that
the sensors that captured them are infallible, or anything about execution that
happened outside Modulo. A signed chain is still a chain of *observations*.

Use the phrase **"offline-verifiable evidence"** - never the bare claim
"independently verifiable" - until the capability this document describes is
actually shipped.

## How a pack is produced

`build_evidence_pack` (in `backend/src/modulo/core/evidence_pack/`) assembles a
pack:

1. The organisation's audit events are written into the bundle file
   `audit-events.ndjson` (one canonical JSON event per line).
2. The chain is re-derived from those events, producing the chain summary
   (`valid`, `event_count`, `head_hash`, `first_gap_index`, `first_tampered_id`,
   `truncated`).
3. A **canonical manifest** is built: pack version, organisation id, exporter
   identity, creation time, run ids, the file list with per-file SHA-256 + size,
   and the chain summary.
4. The manifest is serialised canonically (compact JSON, sorted keys,
   `ensure_ascii=False`, UTF-8) and signed with the instance's Ed25519 key.
5. The pack is returned as a self-contained object: `pack_version`, `manifest`,
   `signature`, `verification_key` and the file bytes.

The `GET /api/v1/admin/audit/evidence-pack` endpoint produces a pack over the
organisation's audit chain (gated by `audit_viewer` + `audit.manage`).

## Publishing the verification key

The public key is derived from the configured signing key and is published two
ways:

- **In the pack** - the `verification_key` section carries the PEM public key,
  so the artefact is self-describing.
- **As an endpoint** - `GET /api/v1/admin/audit/verification-key` returns the
  PEM public key and its `key_id` fingerprint.

Verification always uses the **caller-supplied, pre-published key**, never the
one embedded in the artefact. If a pack's embedded key differs from the supplied
key, verification fails - so a pack cannot be silently re-keyed.

## Verifying a pack offline

Given the pack and the public key, verification needs nothing else:

```
modulo evidence verify <pack.json> --public-key <published-key.pem>
```

The command exits 0 when the pack is valid and 1 otherwise. `--json` emits the
per-check breakdown. The command imports no database, Redis or settings - it is
genuinely offline.

The same check is available programmatically via `verify_evidence_pack`, which
returns an `OfflineVerificationResult` broken out by check.

## Key configuration

Signing is disabled by default and **fails closed**: an instance that has not
opted in cannot emit signed evidence.

- `MODULO_EVIDENCE_SIGNING_KEY` - a PEM-encoded Ed25519 private key. Set it to
  enable signing; the matching public key is derived and published
  automatically.
- `MODULO_EVIDENCE_EXPORTER_IDENTITY` - an optional human-readable identity
  stamped into the signed manifest (defaults to `modulo`).

Key rotation is out of scope for this capability.

## Out of scope (explicitly)

- Changing the audit-chain format or the existing hashing.
- Any vendor phone-home or Farnalabs-operated verification service.
- Export-UI redesign beyond the key/verify affordance.
