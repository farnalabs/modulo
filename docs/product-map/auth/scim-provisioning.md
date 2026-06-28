---
id: feat-auth-scim
prd: 9.2, 9.4
delivery-tasks: [task-nv1-team-api-keys]
bdd:
code:
  - backend/src/modulo/api/routes/scim.py
  - backend/src/modulo/db/crud/scim.py
  - backend/src/modulo/auth/scim_auth.py
unit-tests:
depends-on: [feat-auth-api-keys, feat-teams-entity]
status: partial
---

# SCIM Provisioning (SCIM 2.0)

Maps SCIM Users → internal User, SCIM Groups → internal Team + TeamMembership.
Authenticated via `MODULO_SCIM_TOKEN`, gated by `MODULO_LICENSE_KEY`.

## Behaviours

### Users — Happy Path
- [ ] Create user → 201, valid SCIM User schema returned
- [ ] Get user by id → 200
- [ ] List users → 200, SCIM ListResponse with pagination
- [ ] PUT (full replace) → 200, all attributes updated
- [ ] PATCH (partial update) → 200, only specified fields changed
- [ ] Delete → 204, user deactivated (set active=false)

### Users — Edge Cases
- [ ] Duplicate `userName` → 409 Conflict
- [ ] Create with `externalId` → stored for re-provisioning matching
- [ ] PUT after PATCH → full replace semantics respected
- [ ] PATCH multi-op, one fails → atomic rollback (currently partial-persist bug: line 333-358 mutates in-memory then flushes; mid-list failure leaves dirty state)
- [ ] PATCH `add` on existing field → overwritten (per RFC 7644)
- [ ] PATCH `remove` on nonexistent field → no-op, not error
- [ ] Filter: `userName Eq "foo"` → case-insensitive match working
- [ ] Filter: `active eq true` → boolean filter parses correctly
- [ ] Filter: unsupported attribute → 400 with SCIM Error schema
- [ ] Concurrent create of same userName → exactly one 201, rest 409
- [ ] `startIndex` > total results → empty Resources, totalResults accurate
- [ ] `count` > 100 → capped at 100 (currently enforces 1-100 in Query param)
- [ ] `count` = 0 → not valid per SCIM spec; current code allows it via Query(ge=1) — verify 400
- [ ] email attribute not in SCIM request → defaults from userName

### Groups — Happy Path
- [ ] Create group → 201, SCIM Group schema returned
- [ ] Create group with members → members included
- [ ] Get group → 200, members listed
- [ ] List groups → 200
- [ ] PUT replaces name AND members → old members removed, new added
- [ ] PATCH add member → member added
- [ ] PATCH remove member → member removed
- [ ] Delete group → 204

### Groups — Edge Cases
- [ ] Duplicate `displayName` → 409 Conflict
- [ ] PUT with empty members → all existing members removed
- [ ] PATCH replace members → old members removed, new added atomically
- [ ] PATCH add duplicate member → idempotent (no-op, not error)
- [ ] Remove non-member → no-op, not error
- [ ] Add member that doesn't exist as User → 404 or skip? (currently skips silently: line 464-471)
- [ ] `members[value eq "invalid-uuid"]` remove → 400
- [ ] Filter: `displayName Eq "Engineering"`

### Cross-Cutting
- [ ] RLS isolation: SCIM provisioned user in org A cannot access org B resources
- [ ] Team-scoped resources: Group maps to Team, member mapping respects org
- [ ] License key expired/missing → 402
- [ ] SCIM token invalid/missing → 401
- [ ] Token valid but org mismatch → 403
- [ ] IdP sends duplicate PUT within same second → idempotency (no 409, no duplicate)
- [ ] SCIM filter special characters: `userName Eq "user+tag@domain.com"`
- [ ] Bulk provisioning: 100 users in rapid succession → rate limited or queued? (current impl: no rate limiting, no queuing)
- [ ] Re-provisioning after offboarding: user was deactivated, IdP re-sends → reactivate

## Not implemented (known gaps)
- `/Bulk` endpoint — SCIM 2.0 Bulk operations (Azure AD uses this)
- `/ResourceTypes` endpoint
- `/Schemas` endpoint
- Enterprise User Schema extension (`urn:ietf:params:scim:schemas:extension:enterprise:2.0:User`)
- `externalId` matching on re-provisioning (currently matches only by `userName`)
- PATCH `path` attribute grammar validation (free-form `path` string, no schema validation)
- SCIM filter syntax parser (raw string passed to CRUD; will silently return empty on complex filters)
- Rate limiting / IdP backpressure
