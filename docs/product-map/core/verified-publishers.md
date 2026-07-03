---
id: feat-core-verified-publishers
prd: 8.14
delivery-tasks: [task-nv8-verified-publishers]
code:
  - backend/src/modulo/db/models/publisher.py
  - backend/src/modulo/db/crud/publisher.py
  - backend/src/modulo/db/migrations/versions/0032_publishers.py
  - backend/src/modulo/db/models/library_primitive.py
  - backend/src/modulo/api/routes/admin.py
  - backend/src/modulo/api/routes/registry.py
  - backend/src/modulo/core/registry/
unit-tests:
  - backend/tests/unit/api/test_admin_publishers.py
  - backend/tests/unit/api/test_registry_publishers.py
  - backend/tests/unit/registry/test_publisher_trust.py
  - backend/tests/unit/api/test_publishers_programming_error.py
depends-on: [feat-core-registry-protocol-v2]
bdd: []
status: partial
---

# Verified Publishers

Trust tiers for community registry primitives: green (verified publisher) and amber (community). Publishers are org-scoped entities managed via the admin API. The `verified` field on library primitives reflects the publisher's tier.

## Behaviours

### Admin publisher CRUD

- [x] Admin creates a green-tier publisher → created with `verified_since` set to current time
- [x] Admin creates an amber-tier publisher → created with `verified_since` null
- [x] Admin lists publishers, optionally filtered by `trust_tier`
- [x] Admin searches publishers by name (case-insensitive `ILIKE`)
- [x] Admin updates a publisher's trust tier from amber → green → `verified_since` auto-set
- [x] Admin updates a publisher's trust tier from green → amber → `verified_since` cleared
- [x] Admin updates other fields (name, email, key, website) without affecting tier
- [x] Admin deletes a publisher

### Registry verify / trust lookup

- [x] Registry verify endpoint returns `trust_tier` and `publisher_name` when `public_key_hex` matches a registered DB publisher
- [x] Registry verify endpoint returns null `trust_tier` when no DB publisher matches the key
- [x] Registry verify endpoint falls back to built-in key when `public_key_hex` not provided
- [x] Registry verify returns `publisher_status` (verified/community/revoked) from in-memory model

### Library primitive verified field

- [ ] Registry library primitives have `verified: true` when the publisher is green-tier
- [ ] Registry library primitives have `verified: false` when the publisher is amber-tier
- [x] Local library primitives always have `verified: null` (enforced by check constraint)

### Trust tier display (amber warning on copy)

- [ ] Copy-to-adapt of an amber-tier primitive shows warning: "This primitive has not been verified by Modulo. Review the prompt template and schema before use."
- [ ] Copy-to-adapt of an amber-tier primitive requires `confirm: true`
- [ ] Copy-to-adapt of a green-tier primitive shows no warning, no extra confirmation

### Error states

- [x] Creating a publisher with a duplicate name (per-org) returns 409
- [x] Creating a publisher with a duplicate public key (per-org) returns 409
- [x] Creating a publisher with an invalid trust_tier value returns 422
- [x] Non-admin user cannot create, update, list, or delete publishers (403)
- [x] Updating a non-existent publisher returns 404
- [x] Deleting a non-existent publisher returns 404
- [x] Registry verify with non-existent slug returns 404

### Edge cases

- [x] Publisher name uniqueness is per-organisation, not global
- [x] Publisher public key uniqueness is per-organisation
- [ ] Updating publisher name to its own current name succeeds (self-match excluded from conflict check)
- [ ] Updating publisher key to its own current key succeeds
- [x] Revoking a non-existent publisher fingerprint returns false (in-memory model)
- [x] Unknown signing key fingerprint returns `community` status
- [x] Built-in Modulo publisher is always `verified` in the in-memory model

### Error Handling

- [x] create_publisher catches ProgrammingError → 501 Not Implemented
- [x] update_publisher catches ProgrammingError → 501 Not Implemented
- [x] delete_publisher catches ProgrammingError → 501 Not Implemented
- [x] list_publishers catches ProgrammingError → 501 Not Implemented

### QA History

- 2026-07-03: Cross-cutting QA (index 93): Marked 25 stale [ ]→[x] implemented behaviours across Admin CRUD, Registry Verify, Error States, and Edge Cases. Added Error Handling section with 4 ProgrammingError→501 catches. Updated Known Gaps. Created unit tests for ProgrammingError handling. Created website docs stub.

## Known Gaps
- No BDD feature file — no `.feature` file exists specifically for verified publishers (only direct unit tests covering CRUD, registry verify, and in-memory trust model)
- `verified` field on LibraryPrimitive is not synced to publisher trust tier — it stores the signature check result, not the publisher's tier. Tier-to-primitive mapping is not implemented (green-tier doesn't cascade `verified=true` to registry primitives)
- No amber warning / `confirm: true` enforcement in the copy-to-adapt flow — the PRD specifies this for v2 but it's not implemented
- No frontend publisher management UI — admin CRUD is API-only
- No trust tier badge display in library browser UI (green/amber indicators)
- No verified publisher application process or key issuance API/UI (v2 roadmap)
- No revocation notification to downstream consumers of revoked publishers
- No update-name-to-self or update-key-to-self tests — code allows self-match (excludes own ID from conflict check) but this path is not covered by unit tests
