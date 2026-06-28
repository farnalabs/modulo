---
id: feat-core-audit-crypto-chain
prd: 8.12
delivery-tasks: [task-nv10-audit-crypto-chain]
  - backend/tests/features/audit/event_recording.feature
  - backend/tests/features/personas/marcus-ciso.feature
code:
  - backend/src/modulo/core/audit_logger/__init__.py
  - backend/src/modulo/core/audit_logger/append_only.py
depends-on: [feat-core-audit-trail]
status: partial
---
# Audit — Cryptographic Hash Chain SHA-256 cryptographic chaining of audit events per organisation, providing
tamper-evident integrity. Each event records the hash of the prior event in
the org's sequence, forming a linked chain. Verification recomputes every
hash and validates against the stored chain head. Builds on the base audit trail (feat-core-audit-trail). The cryptographic
linking is the V2 addition described in PRD 8.12. ## Behaviours ### Hash Computation
- [x] SHA-256 hash computed from canonical JSON of all identity fields: event_type, actor_user_id, resource_type, resource_id, payload_json, request_id, previous_hash, event_id, organisation_id, created_at
- [x] Canonical JSON uses sort_keys=True and separators=(",", ":") — no whitespace, sorted keys
- [x] Hash is deterministic — same inputs always produce the same output
- [x] Hash computed by `_compute_event_hash()` after event is flushed to DB with assigned id and created_at ### Chain Construction
- [x] First event in an org: previous_hash is None
- [x] Subsequent events: previous_hash = SHA-256 hash of the immediately prior event in the same org
- [x] Chain head (AuditChainHead) tracks last_event_hash, last_event_id, and event_count per org
- [x] Chain head upserted on every append — created if first event, updated otherwise
- [x] event_count incremented on each append ### Chain Verification (`verify_chain`)
- [x] Recomputes hash for every event in org order (created_at ASC, id ASC)
- [x] Each event's stored previous_hash must match the recomputed hash of the prior event
- [x] Returns valid: True when all links intact
- [x] Returns valid: False + first_tampered_id + first_gap_index when a mismatch is found
- [x] Detail message includes expected vs actual previous_hash
- [x] Empty chain returns valid: True, total_events=0, checked_events=0
- [x] Validates last recomputed hash against AuditChainHead.last_event_hash
- [x] chain_head_match is None when no chain head exists
- [x] Respects max_events limit (default 10,000) — only checks first N events ### Export & Listing
- [x] Paginated export (offset-based) includes previous_hash per event
- [x] Cursor-based listing includes previous_hash per event
- [x] Batch detail includes previous_hash per event ### BDD Scenarios
- [ ] Given 3 audit events exist: When I verify the audit chain, then each event has a previous_hash linking to the prior event and the chain is valid
- [ ] Given a sequence of 100 audit events: When I verify the hash chain, then each event's hash is derived from the previous event's hash and tampering with any event breaks the chain for all subsequent events ### Edge Cases
- [x] Concurrent event creation under same org — serialized by DB transaction, chain head consistency maintained
- [x] verify_chain with >max_events — only checks first N, reports total_events correctly, may miss breaks beyond limit
- [x] Chain head deleted (ON DELETE SET NULL FK) — AuditChainHead.last_event_id is null, chain still verifiable via recomputation
- [x] Actor user deleted — actor_user_id is None in canonical hash, event still verifiable
- [x] Org with zero events — verification returns valid: True, total_events=0 ### Error Handling
- [x] verify_chain with DB failure — exception propagates (no silent fallback)
- [x] Missing previous_hash on non-first event — detected as chain break, returns first_gap_index=0 ### Security
- [x] SHA-256 linking makes single-event tampering detectable — altering any field changes its hash, breaking the link to the next event
- [x] Chain head stored separately — tampering would also need to update AuditChainHead to avoid detection
- [x] Verification is read-only — no mutation performed during integrity check ## Known Gaps
- verify_chain limited to 10,000 events by default — large orgs may need batched or incremental verification
- No event-level retention policy — chain grows unbounded
- No alerting when verify_chain detects tampering — caller must poll or integrate manually
- V2 cryptographic chaining is documented as V2 in PRD but implementation exists alongside V1 audit trail
- Audit viewer UI is enterprise-gated (V1) — chain export is enterprise; recording is free-tier
