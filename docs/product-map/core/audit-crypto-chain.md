---
id: feat-core-audit-crypto-chain
prd: 8.12
delivery-tasks: [task-nv10-audit-crypto-chain]
bdd:
  - backend/tests/bdd/features/audit/event_recording.feature
  - backend/tests/bdd/features/personas/marcus-ciso.feature
code:
  - backend/src/modulo/core/audit_logger/__init__.py
  - backend/src/modulo/core/audit_logger/append_only.py
depends-on: [feat-core-audit-trail]
unit-tests:
  - backend/tests/unit/audit_logger/test_audit_logger.py
  - backend/tests/unit/audit_logger/test_append_only.py
  - backend/tests/integration/test_audit_append_only.py
status: partial
---

# Audit — Cryptographic Hash Chain

SHA-256 cryptographic chaining of audit events per organisation, providing tamper-evident integrity. Each event records the hash of the prior event in the org's sequence, forming a linked chain. Verification recomputes every hash and validates against the stored chain head. Builds on the base audit trail (feat-core-audit-trail). The cryptographic linking is the V2 addition described in PRD 8.12.

## Behaviours

### Hash Computation

- [x] SHA-256 hash computed from canonical JSON of all identity fields: event_type, actor_user_id, resource_type, resource_id, payload_json, request_id, previous_hash, event_id, organisation_id, created_at
- [x] Canonical JSON uses sort_keys=True and separators=(",", ":") — no whitespace, sorted keys
- [x] Hash is deterministic — same inputs always produce the same output
- [x] Hash computed by `_compute_event_hash()` after event is flushed to DB with assigned id and created_at

### Chain Construction

- [x] First event in an org: previous_hash is None
- [x] Subsequent events: previous_hash = SHA-256 hash of the immediately prior event in the same org
- [x] Chain head (AuditChainHead) tracks last_event_hash, last_event_id, and event_count per org
- [x] Chain head upserted on every append — created if first event, updated otherwise
- [x] event_count incremented on each append

### Chain Verification (`verify_chain`)

- [x] Recomputes hash for every event in org order (created_at ASC, id ASC)
- [x] Each event's stored previous_hash must match the recomputed hash of the prior event
- [x] Returns valid: True when all links intact
- [x] Returns valid: False + first_tampered_id + first_gap_index when a mismatch is found
- [x] Detail message includes expected vs actual previous_hash — `_recompute_chain` returns `(gap_index, tampered_id, expected_hash, stored_hash)` and `_describe_chain_break` builds the human-readable tamper evidence surfaced via `_make_verify_result(detail=...)`
- [x] Empty chain returns valid: True, total_events=0, checked_events=0
- [x] Validates last recomputed hash against AuditChainHead.last_event_hash
- [x] chain_head_match is None when no chain head exists
- [x] Respects max_events limit (default 10,000) — only checks first N events

### Export & Listing

- [x] Paginated export (offset-based) includes previous_hash per event
- [x] Cursor-based listing includes previous_hash per event
- [x] Batch detail includes previous_hash per event

### BDD Scenarios

- [x] Given 3 audit events exist: When I verify the audit chain, then each event has a previous_hash linking to the prior event and the chain is valid
- [x] Given a sequence of 100 audit events: When I verify the hash chain, then each event's hash is derived from the previous event's hash and tampering with any event breaks the chain for all subsequent events
- [x] Given 3 audit events exist: When I GET /api/v1/admin/audit/verify with a broken chain, then the response detail mentions the tampered event id and the expected hash

### Edge Cases

- [x] Concurrent event creation under same org — serialized by DB transaction, chain head consistency maintained
- [x] verify_chain with >max_events — only checks first N, reports total_events correctly, may miss breaks beyond limit
- [x] Chain head deleted (ON DELETE SET NULL FK) — AuditChainHead.last_event_id is null, chain still verifiable via recomputation
- [x] Actor user deleted — actor_user_id is None in canonical hash, event still verifiable
- [x] Org with zero events — verification returns valid: True, total_events=0
- [x] Multiple events with identical `created_at` — order within same timestamp is deterministic via `id ASC` tiebreaker
- [x] Actor user ID passed as UUID object vs string — `_uuid_or_none` handles both, hash consistent regardless of input type
- [x] Large payload in export (10 KB tested) — serialized correctly, no truncation
- [x] `created_at` is None on DB record — export returns `None` in dict, verify skips isoformat with `""` fallback
- [x] Chain head `event_count` mismatch with actual DB count — flagged as `chain_count_mismatch`, chain still verifiable via recomputation
- [x] Chain head missing with events present — `chain_head_match` returns `None`, `no_head_corruption` is False, `valid` is False

### Error Handling

- [x] verify_chain with DB failure — exception propagates (no silent fallback)
- [x] Missing previous_hash on non-first event — detected as chain break, returns gap index of the broken event
- [x] Invalid UUID in batch detail request — skipped with warning log, caller gets fewer results (no crash)
- [x] Invalid cursor in list — silently falls back to first page with warning log (caller gets unexpected page)
- [x] Batch size exceeded — truncated to `BATCH_MAX_SIZE` (100) with warning log
- [x] append_audit_event IntegrityError retry exhaustion — re-raises after 3 attempts with exponential backoff
- [x] append_audit_event with `payload_json=None` — resolved to `{}` via `resolved_payload = payload_json or {}`
- [x] append_audit_event with `actor_user_id=None` / `resource_type=None` / `resource_id=None` — all optional, stored as None in DB
- [x] Chain break verification reports tamper evidence — `_recompute_chain` computes expected vs actual previous_hash and the verify result includes a `detail` message describing the break

### Resilience

- [x] Concurrent appends serialized by `SELECT ... FOR UPDATE` on chain head — prevents forks under Postgres
- Non-Postgres backends (MariaDB, SQLite) — `with_for_update()` is a no-op, concurrent appends could create chain forks (platform limitation: chain-head locking is Postgres-only; MariaDB/SQLite are dev/deprecated backends — see Known Gaps)
- [x] append_audit_event retries on IntegrityError — up to 3 attempts with `0.1s × attempt` backoff, handles concurrent first-event race
- [x] All read operations (export, list, batch, verify) are independent queries — a DB failure in one does not cascade
- [x] Cursor decode failure falls back to first page — caller can retry with fresh cursor

### Security

- [x] SHA-256 linking makes single-event tampering detectable — altering any field changes its hash, breaking the link to the next event
- [x] Chain head stored separately — tampering would also need to update AuditChainHead to avoid detection
- [x] Verification is read-only — no mutation performed during integrity check

## QA History
- **2026-08-15 — distribute (final-pass sweep C)**: Documented the unchecked non-Postgres backend gap in Known Gaps — `with_for_update()` is a no-op on MariaDB/SQLite, so concurrent appends could create chain forks (only Postgres serializes appends). No code change (audit_logger outside this sweep's scope beyond the allowlisted files). Status: partial.
- **2026-08-06 (improve-architecture)**: Resolved the "chain break verification lacks detail message" gap — `_recompute_chain` now returns `(gap_index, tampered_id, expected_hash, stored_hash)` and `verify_chain` surfaces a human-readable `detail` via the new `_describe_chain_break` helper (expected vs actual previous_hash at the first gap; first-event breaks clearly flagged). The `detail` field flows through the `/api/v1/admin/audit/verify` response and is now shown in the admin audit UI (`AdminAuditView` surfaces `data.detail`). Added 3 core unit tests (mid-chain break detail, first-event break detail, valid-chain has no detail), 1 route test (detail passthrough), 1 frontend vitest (detail rendered), and 1 BDD scenario in `event_recording.feature` with 2 new step definitions. Marked both detail-message checkboxes `[ ]→[x]`. 73/73 `test_audit_logger.py` tests + ruff + mypy clean.
- **2026-07-02**: Cross-cutting QA (index 49). Fixed BDD scenario path (scenarios() was resolving to nonexistent bdd/features/audit/, now uses ../../features/ corrected path) for test_alpha_audit.py, test_audit.py, and test_personas.py step files. Fixed check_previous_hash step to handle both _appended_events and verify API response (was passing vacuously for "Audit events have cryptographic chaining" scenario). Added 4 missing step definitions for `@goal-marcus-crypto-chain` scenario (sequence of 100 events, chain verification, tampering detection). Updated unit-tests frontmatter from empty [] to 3 actual test file references. Marked both BDD scenario behaviours [ ]→[x].
- **2026-07-05**: Cross-cutting QA (feat-core-audit-crypto-chain). Verified all 40 behaviours against code. Found 1 inaccurate claim: "Detail message includes expected vs actual previous_hash" — code does not pass `detail` to `_make_verify_result`. Added detailed Error Handling (9 items), Resilience (5 items), and Edge Cases (6 new items) sections. Created website docs stub at `Website/modulo-website/src/docs/audit/audit-crypto-chain.md`.

## Known Gaps
- Non-Postgres backends (MariaDB, SQLite): `with_for_update()` is a no-op, so concurrent appends could create chain forks. Production runs on Postgres (Supabase); MariaDB is deprecated and SQLite is dev-only, so this is a documented platform limitation rather than a supported-deployment bug.
- verify_chain limited to 10,000 events by default — large orgs may need batched or incremental verification
- No event-level retention policy — chain grows unbounded
- No alerting when verify_chain detects tampering — caller must poll or integrate manually
- V2 cryptographic chaining is documented as V2 in PRD but implementation exists alongside V1 audit trail
- Audit viewer UI is team-gated (V1) — chain export is team; recording is free-tier

## QA History (2026-08-15 coverage sweep)
- Confirmed the single unchecked behaviour ("Non-Postgres backends — concurrent appends could fork the chain") is a genuine platform limitation, not a fixable code gap: chain-head serialization relies on Postgres `SELECT ... FOR UPDATE`, which is a no-op on MariaDB/SQLite, but production runs on Postgres, MariaDB is deprecated (2026-07-11), and SQLite is dev-only. Documented in Known Gaps. Status: partial (51/52).
